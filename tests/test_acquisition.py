import json
import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from yt_dlp import YoutubeDL  # pyright: ignore[reportMissingTypeStubs]
from yt_dlp.utils import PostProcessingError  # pyright: ignore[reportMissingTypeStubs]
from ytmusicapi import YTMusic  # pyright: ignore[reportMissingTypeStubs]

from trawl.core.audio import Probe
from trawl.core.catalog import Credit, Medium, Release, ReleaseGroupRef, Track, object_value
from trawl.core.fetch import Candidate, CandidateItem, FetchError, PartialFetchError
from trawl.core.youtube import video_id
from trawl.library.layout import render, resolve_collision, safe_name
from trawl.runner.lock import LockBusy, acquire
from trawl.sources.score import match_items, score
from trawl.sources.ytmusic import CopyAudio, YtDlpDownloader, YTMusicSource, is_wall
from trawl.tag.mapping import fields_for
from trawl.verify.fingerprint import AcoustResult
from trawl.verify.policy import decide

_CREDIT = Credit("The Strokes", "artist", "")
_TRACK = Track("track", "recording", 1, "1", "Last Nite", 194_000, (_CREDIT,), ())
_GROUP = ReleaseGroupRef("group", "Is This It", "Album", (), "2001")


def _release(tracks: tuple[Track, ...]) -> Release:
    return Release(
        "release",
        "Is This It",
        "Official",
        "XW",
        "2001",
        (_CREDIT,),
        _GROUP,
        (Medium(1, "CD", tracks, None, ()),),
        "",
        (),
        (),
    )


def test_audio_extraction_refuses_encoder() -> None:
    with pytest.raises(PostProcessingError, match="without re-encoding"):
        CopyAudio(preferredcodec="best").run_ffmpeg("in.webm", "out.mp3", "libmp3lame", [])


@pytest.mark.parametrize("downloads", ["present", "absent", "empty"])
def test_downloader_prefers_postprocessed_download_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, downloads: str
) -> None:
    original = tmp_path / "audio.webm"
    extracted = tmp_path / "audio.opus"
    original.touch()
    extracted.touch()

    def extract_info(_: YoutubeDL, url: str) -> dict[str, object]:
        assert url == "https://www.youtube.com/watch?v=abcDEF12345"
        info: dict[str, object] = {"filepath": str(original)}
        if downloads == "present":
            info["requested_downloads"] = [{"filepath": str(extracted), "acodec": "opus"}]
        elif downloads == "empty":
            info["requested_downloads"] = []
        return info

    monkeypatch.setattr(YoutubeDL, "extract_info", extract_info)
    downloader = YtDlpDownloader()
    if downloads == "empty":
        with pytest.raises(FetchError, match="extracted audio file"):
            downloader.download("abcDEF12345", tmp_path)
    else:
        assert downloader.download("abcDEF12345", tmp_path) == (
            extracted if downloads == "present" else original
        )


@pytest.mark.parametrize(
    ("text", "wall"),
    [
        ("ERROR: HTTP Error 403: Forbidden", True),
        ("Server returned HTTP 429: Too Many Requests", True),
        ("Sign in to confirm you\u2019re not a bot", True),
        ("sign in to confirm you're not a bot", True),
        ("HTTP status code: 403", True),
        ("HTTP Error 404", False),
        ("video 403 seconds", False),
    ],
)
def test_wall_messages(text: str, wall: bool) -> None:
    assert is_wall(ValueError(text)) == wall


@pytest.mark.parametrize("prefix", ["https://youtu.be/", "https://music.youtube.com/watch?v="])
def test_pin_video_url(prefix: str) -> None:
    assert video_id(prefix + "abcDEF12345") == "abcDEF12345"
    with pytest.raises(ValueError, match="YouTube"):
        video_id("https://example.org/watch?v=abcDEF12345")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("CON", "CON_"),
        ("con.txt", "con_.txt"),
        ("LPT1. ", "LPT1_"),
        ("a/b:x", "a_b_x"),
        ('<>:"/\\|?*\0', "__________"),
        ("..", "_"),
        ("AUXILIARY", "AUXILIARY"),
    ],
)
def test_safe_names(value: str, expected: str) -> None:
    assert safe_name(value) == expected


def test_layout_trim_and_case_insensitive_owner() -> None:
    album = _release((_TRACK,))
    fields = replace(
        fields_for(album, _TRACK), title="T" * 400, album="A" * 300, disambiguation="Deluxe"
    )
    path = render(
        "{albumartist}/{original_year} - {album}{disambiguation}/"
        "{disc_prefix}{track:02} - {title}.{ext}",
        fields,
        ".flac",
        2,
    )
    assert max(map(len, path.parts)) <= 120 and len(path.as_posix()) <= 240
    assert path.suffix == ".flac" and path.name.startswith("1-01 - ")
    assert path.parent.name.endswith("(Deluxe)")
    resolved = resolve_collision(path, "abcdefgh-rest", lambda _: "someone-else")
    assert resolved.parent.name.endswith(" [abcdefgh]")
    assert len(resolved.as_posix()) <= 240


def test_matching_and_score() -> None:
    second = replace(_TRACK, mbid="b", position=2, length_ms=194_800)
    first = CandidateItem("a", None, 194_100, "last nite")
    other = CandidateItem("b", None, 195_000, "Last Nite (2011 Remaster)")
    assert match_items([_TRACK, second], (other, first)) == {"track": first, "b": other}
    candidate = Candidate(
        "ytmusic", "album", "album", "Album", "opus", 256, False, True, (first, other)
    )
    assert score(candidate, [_TRACK, second], {"track": first, "b": other}) == pytest.approx(124.7)
    assert match_items([_TRACK, second], (first,)) == {"track": first}


@pytest.mark.parametrize(
    ("length", "hit", "reliable", "mismatch", "same", "expected"),
    [
        (194_000, AcoustResult(0.8, ("recording",)), False, False, False, "acoustid"),
        (194_000, AcoustResult(0.8, ("merged",)), False, False, True, "acoustid"),
        (194_000, AcoustResult(0.8, ("wrong",)), True, False, False, None),
        (194_000, AcoustResult(0.79, ("wrong",)), True, False, False, "duration"),
        (194_000, AcoustResult(0.99, ()), True, True, False, None),
        (15_000, AcoustResult(1, ("wrong",)), True, False, False, "duration"),
        (15_000, AcoustResult(1, ("recording",)), False, False, False, None),
        (None, AcoustResult(1, ("recording",)), False, False, False, "acoustid"),
        (None, AcoustResult(1, ()), True, False, False, None),
    ],
)
def test_verification(
    length: int | None,
    hit: AcoustResult,
    reliable: bool,
    mismatch: bool,
    same: bool,
    expected: str | None,
) -> None:
    track = replace(_TRACK, length_ms=length)
    audio = Probe("opus", 48000, 2, length or 100, 128)
    result = decide(track, audio, [hit], reliable, mismatch, lambda _: same)
    assert result.verified_by == expected and result.accept == (expected is not None)
    assert decide(track, None, [hit], True, False, lambda _: same).outcome == "rejected_probe"
    if length is not None:
        long = replace(audio, duration_ms=length + 10_000)
        assert decide(track, long, [hit], True, False, lambda _: same).outcome == "rejected_probe"


def test_runner_lock_contains_owner(tmp_path: Path) -> None:
    with acquire(tmp_path), pytest.raises(LockBusy, match=f"pid {os.getpid()}"), acquire(tmp_path):
        pytest.fail("second runner acquired lock")
    with acquire(tmp_path):
        pass
    assert (tmp_path / "trawl.lock").read_bytes().strip(b"\x00\n") == str(os.getpid()).encode()


async def test_ytmusic_api_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    data = object_value(
        json.loads((Path(__file__).parent / "fixtures/ytmusic.json").read_text(encoding="utf-8"))
    )
    details = object_value(data["details"])
    calls: list[str] = []

    def search(_: YTMusic, query: str, *, filter: str) -> object:
        calls.append(f"{filter}:{query}")
        return data[filter]

    def get_album(_: YTMusic, browse: str) -> object:
        return details[browse]

    class Downloader:
        def download(self, video_id: str, into: Path) -> Path:
            pytest.fail("search must not download")

    selected = object_value(details["MPREb_w4FkzanBBnt"])
    tracks = tuple(
        replace(
            _TRACK,
            mbid=str(i),
            position=i,
            number=str(i),
            title=str(raw["title"]),
            length_ms=int(str(raw["duration_seconds"])) * 1000,
        )
        for i, raw in enumerate(cast("list[dict[str, object]]", selected["tracks"]), 1)
    )
    album = _release(tracks)
    monkeypatch.setattr(YTMusic, "search", search)
    monkeypatch.setattr(YTMusic, "get_album", get_album)
    source = YTMusicSource(YTMusic(), Downloader(), None)
    candidates = await source.search(album, list(tracks))
    assert [candidate.ref for candidate in candidates] == ["MPREb_w4FkzanBBnt"]
    assert candidates[0].official and len(candidates[0].items) == len(tracks)
    calls.clear()
    track_candidates = await source.search(album, [_TRACK])
    assert calls == ["albums:The Strokes Is This It", "songs:The Strokes Last Nite"]
    assert any(candidate.kind == "track" and candidate.official for candidate in track_candidates)


@pytest.mark.parametrize(
    ("title", "length", "result_title", "duration", "channel", "kind"),
    [
        (
            "The Modern Age",
            193_426,
            "The Modern Age (Rough Trade Version)",
            193,
            "The Strokes",
            "songs",
        ),
        (
            "Last Nite",
            199_933,
            "Last Nite (Rough Trade Version - The Modern Age B-Side)",
            199,
            "The Strokes",
            "songs",
        ),
        (
            "Barely Legal",
            277_080,
            "Barely Legal (The Modern Age EP) - The Strokes",
            278,
            "samuraiinCfede",
            "videos",
        ),
        ("Last Nite", 194_000, "Last Nite (Official Audio)", 194, "The Strokes - Topic", "songs"),
        (
            "Last Nite (2022 Remaster)",
            194_000,
            "Last Nite (Remastered)",
            194,
            "The Strokes",
            "songs",
        ),
        ("Last Nite (feat. Guest)", 194_000, "Last Nite", 194, "The Strokes", "songs"),
        ("Last Nite", 194_000, "The Strokes - Last Nite (Official Video)", 194, "upload", "videos"),
        (
            "Last Nite (Acoustic)",
            194_000,
            "Last Nite (Acoustic) (Official Audio)",
            194,
            "The Strokes",
            "songs",
        ),
    ],
)
async def test_ytmusic_rankings_for_single_recordings(
    monkeypatch: pytest.MonkeyPatch,
    title: str,
    length: int,
    result_title: str,
    duration: int,
    channel: str,
    kind: str,
) -> None:
    def entry(ref: str, title: str, duration: int, channel: str) -> dict[str, object]:
        return {
            "title": title,
            "videoId": ref,
            "artists": [{"name": channel}],
            "duration_seconds": duration,
        }

    track = replace(_TRACK, title=title, length_ms=length)
    release = replace(_release((track,)), title="The Modern Age")
    base = title.split(" (", 1)[0]

    def search(_: YTMusic, query: str, *, filter: str) -> list[dict[str, object]]:
        if filter == "albums":
            return []
        if filter == "songs" and "B-Side" in result_title and not query.endswith(release.title):
            return [entry("album-version", "Last Nite", 194, "The Strokes")]
        if filter == "videos":
            assert "The Modern Age" in query
            if kind == filter:
                return [
                    entry("upload", f"{base} (Official Video)", duration, "unrelated uploader"),
                    entry("yes", result_title, duration, channel),
                ]
            return []
        return [
            entry("wrong-length", base, duration + 12, "The Strokes"),
            entry("guest-live", f"{base} feat. Guest - Live", duration, "The Strokes"),
            entry("karaoke", f"{base} (Karaoke)", duration, "The Strokes")
            if "feat. Guest" in title
            else entry("remix", f"{base} (Dance Remix)", duration, "The Strokes"),
            entry("cover", f"{base} (Cover)", duration, "The Strokes"),
            entry("yes", result_title, duration, channel)
            if kind == filter
            else entry("sped-up", f"{base} (Sped Up)", duration, "The Strokes"),
        ]

    monkeypatch.setattr(YTMusic, "search", search)
    found = await YTMusicSource(YTMusic(), None, None).search(release, [track])
    assert [candidate.ref for candidate in found] == ["yes"]
    assert match_items([track], found[0].items) == {track.mbid: found[0].items[0]}


@pytest.mark.parametrize(
    "version",
    [
        "Live",
        "Remix",
        "Cover",
        "Karaoke",
        "Instrumental",
        "Sped Up",
        "Slowed",
        "Nightcore",
        "8D",
        "Acoustic",
        "Official Live Audio",
    ],
)
async def test_ytmusic_keeps_unknown_suffix_below_exact_and_excludes_other_versions(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    track = replace(_TRACK, title="Live Forever", length_ms=200_000)

    def search(_: YTMusic, _query: str, *, filter: str) -> list[dict[str, object]]:
        if filter != "songs":
            return []
        return [
            {
                "title": title,
                "videoId": ref,
                "artists": [{"name": credit.name} for credit in track.credits],
                "duration_seconds": duration,
            }
            for ref, title, duration in [
                ("unrelated", "Other Song (Archive Cut)", 200),
                ("unknown", f"{track.title} (Archive Cut)", 200),
                ("different", f"{track.title} ({version})", 200),
                ("exact", track.title, 202),
            ]
        ]

    monkeypatch.setattr(YTMusic, "search", search)
    found = await YTMusicSource(YTMusic(), None, None).search(_release((track,)), [track])
    assert [candidate.ref for candidate in found] == ["exact", "unknown"]
    assert match_items([track], found[1].items) == {track.mbid: found[1].items[0]}


@pytest.mark.tools
async def test_partial_download_preserves_success_before_wall(
    tmp_path: Path,
    audio_file: Path,
) -> None:
    class Downloader:
        def download(self, video_id: str, into: Path) -> Path:
            if video_id == "missing":
                raise FetchError("video unavailable")
            if video_id == "wall":
                raise FetchError("HTTP Error 429: https://private.test/?token=do-not-print")
            assert video_id == "ok"
            target = into / "audio.flac"
            shutil.copyfile(audio_file, target)
            return target

    candidate = Candidate(
        "ytmusic",
        "album",
        "album",
        "Album",
        None,
        None,
        False,
        True,
        tuple(
            CandidateItem(ref, position, 31_000, ref)
            for position, ref in enumerate(
                ("missing", "ok", "wall", "not attempted"),
                1,
            )
        ),
    )
    source = YTMusicSource(YTMusic(), Downloader(), None)
    with pytest.raises(PartialFetchError) as exc:
        await source.fetch(candidate, tmp_path / "stage")
    assert [file.item_ref for file in exc.value.files] == ["ok"]
    assert "FetchError" in exc.value.issues["missing"].reason
    assert exc.value.wall is not None and "429" in exc.value.wall.reason
    assert "https://www.youtube.com/watch" in exc.value.wall.reason
    assert "do-not-print" not in exc.value.wall.reason
