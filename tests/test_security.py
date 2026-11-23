import asyncio
import gzip
import os
import sqlite3
import sys
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from conftest import mbid

from trawl.cli.doctor import _slskd  # pyright: ignore[reportPrivateUsage]
from trawl.config.models import ConfigError, ProxyConfig
from trawl.config.secrets import load_secrets
from trawl.core.catalog import ArtistRef, MbDataError
from trawl.core.fetch import Candidate, CandidateItem, FetchError, PartialFetchError
from trawl.core.paths import unlink_regular, write_atomic
from trawl.db.connect import open_db
from trawl.library.place import cleanup_staging, staging_dir
from trawl.mb.client import MusicBrainzClient
from trawl.net.client import HttpClient, HttpError
from trawl.runner.upgrade import _LibraryTrack, _replace_audio  # pyright: ignore[reportPrivateUsage]
from trawl.sources.local import scan_folder
from trawl.sources.soulseek import SoulseekSource
from trawl.tag.cover import fetch_cover
from trawl.tag.lyrics import fetch_lyrics
from trawl.verify.fingerprint import AcoustId
from trawl.verify.probe import AudioError, command

if sys.platform == "win32":
    from _winapi import CreateJunction


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_redirect_not_sent(method: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host in {"musicbrainz.org", "api.acoustid.org"}
        target = "https" if method == "POST" else "http"
        return httpx.Response(307, headers={"Location": f"{target}://elsewhere.test/data"})

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    try:
        with pytest.raises(HttpError) as failure:
            if method == "POST":
                await AcoustId(http, "test-secret").lookup(1, "fingerprint")
            else:
                await http.get_json("https://musicbrainz.org/start", {"secret": "fixture"})
        assert failure.value.status == 307
        assert "secret" not in str(failure.value)
        assert "fixture" not in str(failure.value)
    finally:
        await http.aclose()


@pytest.mark.parametrize("downgrade", [True, False])
async def test_cover_checks_every_redirect(downgrade: bool) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "coverartarchive.org":
            return httpx.Response(302, headers={"Location": "https://archive.org/mirror"})
        if request.url.path == "/mirror":
            scheme = "http" if downgrade else "https"
            return httpx.Response(
                302, headers={"Location": f"{scheme}://archive.org/cover?token=x"}
            )
        assert request.url.scheme == "https"
        return httpx.Response(200, content=b"art")

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    try:
        if downgrade:
            with pytest.raises(HttpError, match="requires HTTPS") as failure:
                await fetch_cover(http, mbid("release"), mbid("group"))
            assert "CAA" in str(failure.value) and "token" not in str(failure.value)
        else:
            cover = await fetch_cover(http, mbid("release"), mbid("group"))
            assert cover is not None and cover.full == cover.embed == b"art"
    finally:
        await http.aclose()


@pytest.mark.parametrize("service", ["cover", "lyrics"])
async def test_endpoint_size_limit(service: str) -> None:
    limit = 20_000_000 if service == "cover" else 2_000_000

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b" " * (limit + 1)
            pytest.fail("client consumed beyond decoded limit")

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Body())

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    try:
        with pytest.raises(HttpError, match="exceeds decoded size limit"):
            if service == "cover":
                await fetch_cover(http, mbid("release"), mbid("group"))
            else:
                await fetch_lyrics(http, "Artist", "Song", "Album", 120)
    finally:
        await http.aclose()


async def test_decoded_body_is_capped_before_stream_ends() -> None:
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield gzip.compress(b" " * 10_000_001)
            pytest.fail("client consumed beyond decoded limit")

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=Body())

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    try:
        with pytest.raises(HttpError, match="exceeds"):
            await http.get_json("https://musicbrainz.org/start")
    finally:
        await http.aclose()


async def test_transport_error_does_not_disclose_raw_exception() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("transport-private-message", request=request)

    http = HttpClient("test", {}, httpx.MockTransport(handle), max_attempts=1)
    try:
        with pytest.raises(HttpError) as failure:
            await http.get_json("https://musicbrainz.org/start", {"secret": "fixture"})
        message = str(failure.value)
        assert "MusicBrainz" in message and "ConnectError" in message
        assert "https://musicbrainz.org/start" in message
        assert "transport-private-message" not in message and "fixture" not in message
    finally:
        await http.aclose()


def make_symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, PermissionError):
        pytest.skip("OS does not permit creation of symbolic links")
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("OS does not permit creation of symbolic links")
        raise


def test_staging_refuses_directory_link(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    make_symlink(tmp_path / ".trawl-staging", target, directory=True)
    with pytest.raises(OSError, match="link"):
        staging_dir(tmp_path)


def test_staging_cleanup_does_not_enter_linked_directory(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    audio = target / "keep.flac"
    audio.write_bytes(b"owned outside staging")
    root = staging_dir(tmp_path)
    link = root / "linked"
    if sys.platform == "win32":
        CreateJunction(str(target), str(link))
    else:
        make_symlink(link, target, directory=True)
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE tracks (id, path, state)")
        conn.execute("CREATE TABLE current_rejections (path, track_id)")
        cleanup_staging(conn, root)
    assert audio.read_bytes() == b"owned outside staging"
    assert link.is_dir()


def test_staging_cleanup_refuses_linked_root(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / ".trawl-staging"
    if sys.platform == "win32":
        CreateJunction(str(target), str(link))
    else:
        make_symlink(link, target, directory=True)
    with pytest.raises(OSError, match="link"):
        staging_dir(tmp_path)
    with closing(sqlite3.connect(":memory:")) as conn, pytest.raises(OSError, match="link"):
        cleanup_staging(conn, link)


def test_atomic_sidecar_never_writes_through_a_link(tmp_path: Path) -> None:
    target = tmp_path / "outside.txt"
    target.write_text("keep", encoding="utf-8")
    sidecar = tmp_path / "track.lrc"
    if os.name == "nt":
        os.link(target, sidecar)
    else:
        make_symlink(sidecar, target)
    write_atomic(sidecar, b"new lyrics")
    assert target.read_text(encoding="utf-8") == "keep"
    assert sidecar.read_bytes() == b"new lyrics"


def test_unlink_regular_skips_links(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "linked"
    if sys.platform == "win32":
        CreateJunction(str(target), str(link))
    else:
        make_symlink(link, target, directory=True)
    assert not unlink_regular(link)
    assert link.is_dir() and target.is_dir()
    assert "Skipping linked path" in caplog.text


def test_upgrade_does_not_move_original_from_linked_directory(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    original = target / "track.mp3"
    original.write_bytes(b"original")
    link = tmp_path / "linked"
    if sys.platform == "win32":
        CreateJunction(str(target), str(link))
    else:
        make_symlink(link, target, directory=True)
    staged, final = tmp_path / "staged.flac", tmp_path / "final.flac"
    staged.write_bytes(b"new")
    with (
        closing(sqlite3.connect(":memory:")) as conn,
        pytest.raises(OSError, match="symlink or junction"),
    ):
        _replace_audio(conn, _LibraryTrack(1, link / original.name), staged, final)
    assert original.read_bytes() == b"original"
    assert not final.exists()


def _soulseek_candidate(size: int) -> Candidate:
    return Candidate(
        "soulseek",
        "peer",
        "track",
        "Track",
        "flac",
        None,
        True,
        False,
        (CandidateItem("Shared/Track.flac", 1, 9_999_999, "Track", size=size),),
    )


async def test_soulseek_rejects_oversize_before_queueing(tmp_path: Path) -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        pytest.fail("oversized candidate was sent to slskd")

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    source = SoulseekSource(http, "http://localhost:5030", "test", tmp_path)
    try:
        with pytest.raises(PartialFetchError) as failure:
            await source.fetch(_soulseek_candidate(200_000_001), tmp_path)
        assert "size limit" in failure.value.issues["Shared/Track.flac"].reason
    finally:
        await http.aclose()


async def test_soulseek_candidate_timeout_even_with_progress(tmp_path: Path) -> None:
    bytes_so_far = 0
    deleted = False

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal bytes_so_far, deleted
        if request.method == "DELETE":
            deleted = True
            return httpx.Response(204)
        if request.method == "POST":
            return httpx.Response(201)
        bytes_so_far += 1
        return httpx.Response(
            200,
            json=[
                {
                    "id": "token",
                    "filename": "Shared/Track.flac",
                    "state": "InProgress",
                    "bytesTransferred": bytes_so_far,
                },
            ],
        )

    async def tick(_: float) -> None:
        await asyncio.sleep(0.005)

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    source = SoulseekSource(
        http, "http://localhost:5030", "test", tmp_path, candidate_timeout_s=0.05, sleep=tick
    )
    try:
        with pytest.raises(FetchError, match="candidate timed out"):
            await source.fetch(_soulseek_candidate(200_000_000), tmp_path)
        assert bytes_so_far > 1
        assert deleted
    finally:
        await http.aclose()


def test_slow_command_errors_instead_of_hanging() -> None:
    with pytest.raises(AudioError, match="timed out"):
        command(sys.executable, "-c", "import time; time.sleep(2)", timeout=0.05)


def test_subprocess_capture_is_bounded() -> None:
    with pytest.raises(AudioError, match="capture limit"):
        command(sys.executable, "-c", "import sys; sys.stderr.write('x' * 1048577)")


async def test_acoustid_rejects_non_uuid_recording() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "ok", "results": [{"score": 0.9, "recordings": [{"id": "../"}]}]}
        )

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    try:
        with pytest.raises(HttpError, match="invalid recording UUID"):
            await AcoustId(http, "test").lookup(1, "test")
    finally:
        await http.aclose()


async def test_deep_json_fails_as_http_error() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        depth = 200_000  # beyond any interpreter recursion limit on every platform
        return httpx.Response(200, content=b"[" * depth + b"]" * depth)

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    try:
        with pytest.raises(HttpError, match="RecursionError"):
            await http.get_json("https://musicbrainz.org/")
    finally:
        await http.aclose()


async def test_musicbrainz_invalid_item_does_not_hide_valid_sibling(tmp_path: Path) -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"artists": [{"id": "../bad", "name": "Bad"}, {"id": mbid("a"), "name": "A"}]},
        )

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    try:
        with closing(open_db(tmp_path / "test.sqlite")) as conn:
            artists = await MusicBrainzClient(http, conn, 0).search_artists("a")
            assert [artist.mbid for artist in artists] == [mbid("a")]
    finally:
        await http.aclose()


def test_local_scan_skips_linked_disc(tmp_path: Path) -> None:
    root = tmp_path / "album"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "track.cue").touch()
    if sys.platform == "win32":
        CreateJunction(str(outside), str(root / "Disc 1"))
    else:
        make_symlink(root / "Disc 1", outside, directory=True)
    assert scan_folder(root) == []
    assert scan_folder(root / "Disc 1") == []


async def test_doctor_explains_insecure_slskd(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    secrets = config.with_name("secrets.toml")
    secrets.write_text(
        'slskd_api_key = "private"\nslskd_url = "http://192.0.2.1:5030"\n',
        encoding="utf-8",
    )
    proxy = ProxyConfig()
    blocked = await _slskd(config, tmp_path, offline=True, proxy=proxy)
    assert blocked.status == "FAIL" and "HTTPS" in blocked.detail
    with secrets.open("a", encoding="utf-8") as stream:
        stream.write("slskd_allow_insecure = true\n")
    allowed = await _slskd(config, tmp_path, offline=True, proxy=proxy)
    assert allowed.status == "WARNING" and "plaintext" in allowed.detail
    assert "private" not in allowed.detail


def test_rejects_non_uuid_mbids() -> None:
    with pytest.raises(MbDataError, match="UUID"):
        ArtistRef.from_json({"name": "Artist", "id": "../bad"})


def test_slskd_remote_http_requires_explicit_permission(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    secret = tmp_path / "secrets.toml"
    secret.write_text('slskd_url = "http://192.0.2.1:5030"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="slskd_allow_insecure"):
        load_secrets(cfg)
    with secret.open("a", encoding="utf-8") as stream:
        stream.write("slskd_allow_insecure = true\n")
    assert load_secrets(cfg).slskd_url == "http://192.0.2.1:5030"


def test_secrets_warn_on_posix(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    secret = tmp_path / "secrets.toml"
    secret.write_text('acoustid_key = "do-not-print"\n', encoding="utf-8")
    secret.chmod(0o644)
    assert load_secrets(tmp_path / "config.toml").acoustid_key == "do-not-print"
    assert ("group/world readable" in caplog.text) == (sys.platform != "win32")
    assert "do-not-print" not in caplog.text
