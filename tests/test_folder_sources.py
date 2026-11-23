import sqlite3
from contextlib import aclosing, closing
from dataclasses import replace
from pathlib import Path, PurePosixPath

import httpx
import pytest

from trawl.library.repack import repack
from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import Credit, Medium, Release, ReleaseGroupRef, Track
from trawl.net.client import HttpClient
from trawl.runner.check import check_folder
from trawl.runner.tools import AudioTools
from trawl.sources.base import Fetched
from trawl.sources.folder import (
    FolderFile,
    group_disc_subfolders,
    match_folder,
    pick_format_set,
    reject_cue_image,
)
from trawl.sources.local import LocalSource
from trawl.verify.fingerprint import AcoustId
from trawl.verify.probe import AudioError, Probe, command, probe

CREDIT = Credit("The Band", "artist", "")


def album() -> Release:
    media = tuple(
        Medium(
            disc,
            "CD",
            (Track(f"track-{disc}", f"rec-{disc}", 1, "1", title, 4000, (CREDIT,), ()),),
            None,
            (),
        )
        for disc, title in enumerate(("First Song", "Second Song"), 1)
    )
    return Release(
        "edition",
        "The Album",
        "Official",
        "XW",
        "2001",
        (CREDIT,),
        ReleaseGroupRef("group", "The Album", "Album", (), "2001"),
        media,
        "",
        (),
        (),
    )


def member(name: str, length: int = 4000) -> FolderFile:
    path = PurePosixPath(name)
    return FolderFile(path, 200, length, 320, path.suffix)


def test_disc_groups_prefer_lossless_and_ignore_extras() -> None:
    release = album()
    files = group_disc_subfolders(
        [
            member("CD1/01 first song.mp3"),
            member("CD1/01 first song.flac"),
            member("Disc 2/01 second song.flac"),
            member("Disc 2/01 second song.mp3"),
            member("cover.jpg"),
            member("rip.log"),
        ]
    )
    chosen = pick_format_set(files)
    mapping = match_folder(
        release, [track for disc in release.media for track in disc.tracks], chosen
    )
    assert {key: value.rel_path.as_posix() for key, value in mapping.items()} == {
        "track-1": "CD1/01 first song.flac",
        "track-2": "Disc 2/01 second song.flac",
    }
    assert reject_cue_image([member("album.wav"), member("album.cue")]) == "cue image not supported"


def tone(path: Path, codec: str, freq: int = 440) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run = command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={freq}:duration=4",
        "-c:a",
        codec,
        "-metadata",
        "artist=The Band",
        "-metadata",
        "album=The Album",
        str(path),
    )
    assert run.returncode == 0, run.stderr


@pytest.mark.tools
def test_repack_accepts_integer_wav_and_rejects_float(tmp_path: Path) -> None:
    wav, floating, flac = (tmp_path / name for name in ("audio.wav", "float.wav", "audio.flac"))
    tone(wav, "pcm_s24le")
    tone(floating, "pcm_f32le")
    repack(wav, flac)
    assert probe(flac).codec == "flac"
    with pytest.raises(AudioError, match="float pcm"):
        repack(floating, tmp_path / "must-not-exist.flac")


@pytest.mark.tools
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("directory", "move"), [("The Band - The Album", False), ("purchased", True)]
)
async def test_local_finds_folder_and_stages_only_audio(
    tmp_path: Path, directory: str, move: bool
) -> None:
    release = album()
    inbox = tmp_path / "inbox"
    root = inbox / directory
    originals = (root / "CD1" / "01 First Song.flac", root / "CD 2" / "01 Second Song.flac")
    for original in originals:
        tone(original, "flac")
    art = root / "cover.jpg"
    art.touch()
    source = LocalSource((inbox,), move=move)
    candidates = await source.search(
        release, [track for disc in release.media for track in disc.tracks]
    )
    assert len(candidates) == 1
    stage = tmp_path / "stage"
    stage.mkdir()
    files = await source.fetch(candidates[0], stage)
    assert len(files) == 2 and all(file.path.is_file() for file in files)
    assert all(file.exists() != move for file in originals)
    assert art.is_file() and not (stage / "cover.jpg").exists()


@pytest.mark.tools
def test_repack_lossless_wavpack_under_bitrate_cutoff(tmp_path: Path) -> None:
    source = tmp_path / "quiet.wv"
    tone(source, "wavpack")
    flac = tmp_path / "quiet.flac"
    repack(source, flac)
    assert probe(flac).codec == "flac"
    with pytest.raises(AudioError, match="destination already exists"):
        repack(source, flac)
    assert probe(flac).codec == "flac"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["swap", "reject"])
async def test_fingerprints_override_folder_order_and_guard_duration(mode: str) -> None:
    tracks = {
        track.mbid: replace(
            track, length_ms=31_000 if mode == "swap" else None if disc.position == 1 else 31_000
        )
        for disc in album().media
        for track in disc.tracks
    }
    files = {key: Fetched(key, Path(key), "flac", None, True) for key in tracks}
    tools = AudioTools(
        probe=lambda _: Probe("flac", 44100, 2, 31_000, None),
        decode_ok=lambda _: True,
        fingerprint=lambda path: (31, path.name),
    )

    def request(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "api.acoustid.org"
        result: list[dict[str, object]] = []
        if mode == "swap":
            rec = "rec-2" if b"fingerprint=track-1" in req.content else "rec-1"
            result = [{"score": 0.99, "recordings": [{"id": rec}]}]
        return httpx.Response(200, json={"status": "ok", "results": result})

    with closing(sqlite3.connect(":memory:")) as conn:
        async with aclosing(
            HttpClient("tests", {}, transport=httpx.MockTransport(request))
        ) as http:
            checked = await check_folder(
                tracks,
                files,
                True,
                AcoustId(http, "fixture"),
                MusicBrainzClient(http, conn, 0),
                0.8,
                tools,
            )
    if mode == "swap":
        assert checked["track-1"][0] == files["track-2"]
        assert checked["track-2"][0] == files["track-1"]
        assert all(result.decision.verified_by == "acoustid" for _, result in checked.values())
    else:
        assert all(result.decision.outcome == "rejected_verify" for _, result in checked.values())
        assert (
            checked["track-2"][1].decision.reason
            == "another file in this candidate was rejected by AcoustID"
        )
