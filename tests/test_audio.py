import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from mutagen import File  # pyright: ignore[reportMissingTypeStubs,reportUnknownVariableType]

from trawl.config import GenreConfig
from trawl.mb.models import Credit, Medium, Release, ReleaseGroupRef, Track, object_value
from trawl.net.client import HttpClient, HttpError
from trawl.tag.cover import fetch_cover
from trawl.tag.lyrics import Lyrics
from trawl.tag.mapping import TagFields, fields_for
from trawl.tag.replaygain import supports_opus, track_gain
from trawl.tag.writer import write
from trawl.verify.fingerprint import AcoustId, AcoustResult
from trawl.verify.probe import command, probe


@pytest.fixture
def edition() -> Release:
    band = (Credit("Artist", "artist", " feat. "), Credit("Guest", "guest", ""))
    track = Track("track", "recording", 1, "1", "Title", 31_000, band, ("AAABB0100001",))
    return Release(
        "release",
        "Album",
        "Official",
        "XW",
        "2005-04-12",
        band,
        ReleaseGroupRef("group", "Album", "Album", ("Compilation",), "2001"),
        (Medium(1, "CD", (track,), None, ()),),
        "Bonus",
        ("Label",),
        ("hard rock", "rock"),
    )


@pytest.fixture
def fields(edition: Release) -> TagFields:
    return fields_for(edition, edition.media[0].tracks[0])


@pytest.mark.parametrize(
    ("genres", "fallback", "config", "expected"),
    [
        (("rock",), ("other",), GenreConfig(), ("rock",)),
        ((), ("jazz", "rock", "pop", "folk"), GenreConfig(), ("jazz", "rock", "pop")),
        (("hard rock", "rock"), (), GenreConfig({"hard rock": "Rock", "rock": "Rock"}), ("Rock",)),
        (("rock",), (), GenreConfig(artist={"artist": "Manual"}), ("Manual",)),
    ],
)
def test_genres(
    edition: Release,
    genres: tuple[str, ...],
    fallback: tuple[str, ...],
    config: GenreConfig,
    expected: tuple[str, ...],
) -> None:
    release = replace(edition, genres=genres)
    assert fields_for(release, release.media[0].tracks[0], config, fallback).genres == expected


@pytest.mark.tools
@pytest.mark.parametrize(
    ("ext", "encoder", "format_name", "codec"),
    [
        ("flac", "flac", "vorbis", "flac"),
        ("opus", "libopus", "vorbis", "opus"),
        ("ogg", "libvorbis", "vorbis", "vorbis"),
        ("m4a", "aac", "mp4", "aac"),
        ("mp3", "libmp3lame", "id3", "mp3"),
    ],
)
def test_picard_names_and_codec(
    tmp_path: Path,
    fields: TagFields,
    ext: str,
    encoder: str,
    format_name: str,
    codec: str,
    jpeg: bytes,
) -> None:
    path = tmp_path / f"audio.{ext}"
    created = command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=d=5",
        "-c:a",
        encoder,
        "-metadata",
        "comment=junk",
        str(path),
    )
    assert created.returncode == 0, created.stderr
    write(path, fields, jpeg, "Lyrics")
    mapping = object_value(
        json.loads(
            (Path(__file__).parent / "fixtures/picard_mapping.json").read_text(encoding="utf-8")
        )
    )
    names = {str(object_value(value)[format_name]) for value in mapping.values()}
    audio: Any = File(path)
    tags: Any = audio.tags
    actual = set(tags.keys())
    if format_name == "vorbis":
        actual = {str(key).upper() for key in actual}
    if ext == "flac":
        assert audio.pictures[0].data == jpeg
        names.remove("METADATA_BLOCK_PICTURE")
    assert actual == names
    assert probe(path).codec == codec
    if ext in {"flac", "opus"}:
        assert supports_opus()
        track_gain(path)
        reread: Any = File(path)
        if ext == "opus":
            assert reread["R128_TRACK_GAIN"]
            assert not any(str(key).upper().startswith("REPLAYGAIN_") for key in reread)
        else:
            assert reread["REPLAYGAIN_TRACK_GAIN"]


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("match", [AcoustResult(0.98, ("recording",))]),
        ("empty", []),
        ("no_recordings", [AcoustResult(0.98, ())]),
    ],
)
async def test_acoustid_fixtures(fixture: str, expected: list[AcoustResult]) -> None:
    payload = object_value(
        json.loads((Path(__file__).parent / "fixtures/acoustid.json").read_text(encoding="utf-8"))
    )

    def reply(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.content == b"client=test&duration=31&fingerprint=abcd&meta=recordingids"
        return httpx.Response(200, json=payload[fixture])

    http = HttpClient("tests", {}, httpx.MockTransport(reply))
    try:
        assert await AcoustId(http, "test").lookup(31, "abcd") == expected
    finally:
        await http.aclose()


async def test_cover_fallback_and_size() -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/release/"):
            return httpx.Response(404)
        if request.url.path.endswith("front-1200"):
            return httpx.Response(200, content=b"small")
        if request.url.path.endswith("/bad"):
            return httpx.Response(200, content=b"cut", headers={"Content-Length": "50"})
        return httpx.Response(200, content=b"large")

    http = HttpClient("tests", {}, httpx.MockTransport(reply))
    try:
        cover = await fetch_cover(http, "release", "group")
        assert cover is not None and cover.full == b"large" and cover.embed == b"small"
        with pytest.raises(HttpError, match="Content-Length"):
            await http.get_bytes("https://coverartarchive.org/bad")
    finally:
        await http.aclose()


def test_synced_lyrics_lose_timestamps_only_when_embedded() -> None:
    lyrics = Lyrics("[00:05.00] Line one\n[00:07.00][01:01]Line two", None)
    assert lyrics.embedded == "Line one\nLine two"
    assert lyrics.synced and lyrics.synced.startswith("[")
