import json
import shutil
from collections.abc import Callable
from contextlib import closing
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import mbid
from mutagen.flac import FLAC  # pyright: ignore[reportMissingTypeStubs]
from typer.testing import CliRunner

from trawl import cli
from trawl.db.connect import open_db
from trawl.net.client import HttpClient
from trawl.sources.ytmusic import YtDlpDownloader
from trawl.verify.probe import command, probe

RELEASE = "3b7c026a-4256-4448-b234-abcdef000010"
GROUP = "3b7c026a-4256-4448-b234-abcdef000011"
TITLES = ("First Song", "Second Song", "Third Song")


def _release(lengths: tuple[int, ...], discs: int) -> dict[str, object]:
    tracks = [
        {
            "id": mbid(f"track-{i}"),
            "recording": {"id": mbid(f"rec-{i}")},
            "title": TITLES[i - 1],
            "number": str(i if discs == 1 else 1),
            "position": i if discs == 1 else 1,
            "length": duration * 1000,
        }
        for i, duration in enumerate(lengths, 1)
    ]
    return {
        "id": RELEASE,
        "title": "The Album",
        "status": "Official",
        "country": "XW",
        "date": "2001",
        "artist-credit": [
            {"name": "The Band", "artist": {"name": "The Band", "id": mbid("artist")}}
        ],
        "release-group": {
            "id": GROUP,
            "title": "The Album",
            "primary-type": "Album",
            "first-release-date": "2001",
        },
        "media": [
            {
                "position": i + 1,
                "format": "CD",
                "tracks": tracks if discs == 1 else tracks[i : i + 1],
            }
            for i in range(discs)
        ],
        "genres": [{"name": "rock", "count": 1}],
    }


def _audio(path: Path, codec: str, duration: int, frequency: int) -> None:
    path.parent.mkdir(exist_ok=True, parents=True)
    result = command(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={frequency}:duration={duration}",
        "-c:a",
        codec,
        str(path),
    )
    assert result.returncode == 0, result.stderr


def _config(root: Path, sources: str) -> Path:
    path = root / "config.toml"
    path.write_text(
        f'contact_email="a@b"\nlibrary_root="{(root / "library").as_posix()}"\n'
        f'data_dir="{(root / "data").as_posix()}"\n[sources]\norder={sources}\n'
        f'[sources.soulseek]\ndownload_dir="{(root / "download").as_posix()}"\n'
        "[lyrics]\nenabled=false\n",
        encoding="utf-8",
    )
    path.with_name("secrets.toml").write_text(
        'acoustid_key="fixture"\nslskd_url="https://slskd.test"\nslskd_api_key="fixture"\n',
        encoding="utf-8",
    )
    return path


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, handle: Callable[[httpx.Request], httpx.Response]
) -> None:
    def factory(agent: str, _: object) -> HttpClient:
        return HttpClient(agent, {}, transport=httpx.MockTransport(handle), max_attempts=1)

    monkeypatch.setattr(cli, "HttpClient", factory)


def _invoke(config: Path, *args: str) -> str:
    result = CliRunner().invoke(cli.app, ["--config", str(config), *args])
    assert result.exit_code == 0, (result.output, result.exception)
    return result.output


@pytest.mark.tools
@pytest.mark.parametrize("scenario", ["direct", "upgrade"])
def test_slskd_two_disc_album_and_cli_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    config = _config(tmp_path, '["soulseek", "ytmusic"]')
    release = _release((4, 4), 2)
    peer = PureWindowsPath("Music/The Band/The Album")
    flacs = {
        str(peer / f"CD{i}" / f"01 {title}.flac"): tmp_path / "sources" / f"{i}.flac"
        for i, title in enumerate(TITLES[:2], 1)
    }
    requests: dict[str, int] = {}
    for i, source in enumerate(flacs.values()):
        _audio(source, "flac", 4, 440 + 220 * i)
    opus = tmp_path / "yt.opus"
    _audio(opus, "libopus", 4, 440)
    can_download = scenario == "direct"

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "coverartarchive.org":
            return httpx.Response(404)
        if path == f"/ws/2/release/{RELEASE}":
            return httpx.Response(200, json=release)
        if path == "/ws/2/release":
            assert request.url.params["release-group"] == GROUP
            return httpx.Response(200, json={"releases": [release], "release-count": 1})
        assert request.url.host == "slskd.test" and request.headers["X-API-Key"] == "fixture"
        if path == "/api/v0/searches":
            if not can_download:
                return httpx.Response(401)
            assert json.loads(request.content)["searchText"] == "The Band The Album"
            return httpx.Response(201)
        if path.endswith("/responses"):
            return httpx.Response(
                200,
                json=[
                    {
                        "username": "peer",
                        "hasFreeUploadSlot": True,
                        "uploadSpeed": 42,
                        "files": [
                            {
                                "filename": filename,
                                "size": src.stat().st_size,
                                "length": 4,
                                "extension": "flac",
                            }
                            for filename, src in flacs.items()
                        ]
                        + [
                            {
                                "filename": str(peer / name),
                                "size": 100,
                                "extension": name.split(".")[1],
                            }
                            for name in ("rip.log", "cover.jpg")
                        ],
                    }
                ],
            )
        if "/searches/" in path:
            return (
                httpx.Response(204)
                if request.method == "DELETE"
                else httpx.Response(200, json={"isComplete": True})
            )
        assert path == "/api/v0/transfers/downloads/peer"
        if request.method == "POST":
            requests.update((row["filename"], row["size"]) for row in json.loads(request.content))
            assert set(requests) == set(flacs)
            for filename, size in requests.items():
                remote, src = PureWindowsPath(filename), flacs[filename]
                dest = tmp_path / "download" / remote.parent.name / remote.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                assert src.stat().st_size == size
                shutil.copyfile(src, dest)
            return httpx.Response(201)
        return httpx.Response(
            200,
            json={
                "directories": [
                    {
                        "files": [
                            {
                                "id": str(i),
                                "filename": name,
                                "state": "Completed, Succeeded",
                                "bytesTransferred": size,
                            }
                            for i, (name, size) in enumerate(requests.items())
                        ]
                    }
                ]
            },
        )

    def search(_: object, query: str, filter: str) -> list[dict[str, str]]:
        assert (
            scenario == "upgrade"
            and not can_download
            and query == "The Band The Album"
            and filter == "albums"
        )
        return [{"browseId": "fixture"}]

    def get_album(_: object, browse_id: str) -> dict[str, object]:
        assert browse_id == "fixture"
        return {
            "artists": [{"name": "The Band"}],
            "tracks": [
                {"videoId": str(i), "title": title, "duration_seconds": 4, "trackNumber": i}
                for i, title in enumerate(TITLES[:2], 1)
            ],
        }

    def get_lossy(_: YtDlpDownloader, video_id: str, into: Path) -> Path:
        return Path(shutil.copyfile(opus, into / f"{video_id}.opus"))

    _install_transport(monkeypatch, handle)
    monkeypatch.setattr(cli.YTMusic, "search", search)
    monkeypatch.setattr(cli.YTMusic, "get_album", get_album)
    monkeypatch.setattr(YtDlpDownloader, "download", get_lossy)
    _invoke(config, "add", f"https://musicbrainz.org/release/{RELEASE}", "--yes")
    assert "2 tracks added" in _invoke(config, "plan")
    result = _invoke(config, "run", "--once")
    assert "done" in result and "soulseek" in result
    with closing(open_db(tmp_path / "data" / "trawl.sqlite")) as conn:
        if scenario == "upgrade":
            assert "Wall: soulseek" in result
            old = [
                Path(row[0])
                for row in conn.execute(
                    "SELECT path FROM tracks WHERE codec='opus' AND state='done'"
                )
            ]
            assert len(old) == 2 and all(path.is_file() for path in old)
            with conn:
                conn.execute("UPDATE source_state SET paused_until='2000-01-01'")
                conn.execute("UPDATE releases SET upgrade_checked_at=datetime('now')")
            can_download = True
            assert "tracks upgraded: 0" in _invoke(config, "upgrade")
            assert "tracks upgraded: 2" in _invoke(config, "upgrade", "--now")
            assert not any(path.exists() for path in old)
        rows = [
            tuple(row)
            for row in conn.execute(
                "SELECT path,codec,lossless,source FROM tracks WHERE state='done' ORDER BY disc"
            )
        ]
        assert len(rows) == 2
        for disc, (raw, codec, lossless, source) in enumerate(rows, 1):
            assert (codec, lossless, source) == ("flac", 1, "soulseek")
            path = Path(raw)
            assert probe(path).codec == "flac"
            tags = FLAC(path)
            assert tags["discnumber"] == [str(disc)] and tags["tracknumber"] == ["1"]
            assert tags["musicbrainz_trackid"] == [mbid(f"rec-{disc}")]
            assert tags["replaygain_album_gain"]
        assert not list((tmp_path / "library").rglob("*.log"))


@pytest.mark.tools
def test_cli_import_fingerprints_three_unrelated_filenames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, '["local"]')
    lengths = (31, 36, 41)
    release = _release(lengths, 1)
    folder = tmp_path / "unknown-files"
    for i, duration in enumerate(lengths, 1):
        _audio(folder / f"unnamed-{4 - i}.flac", "flac", duration, 220 * i)
    fingerprints: dict[int, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "coverartarchive.org":
            return httpx.Response(404)
        if request.url.host == "api.acoustid.org":
            form = parse_qs(request.content.decode())
            duration = int(form["duration"][0])
            fp = form["fingerprint"][0]
            assert fingerprints.setdefault(duration, fp) == fp
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "results": [
                        {
                            "score": 0.99,
                            "recordings": [{"id": mbid(f"rec-{lengths.index(duration) + 1}")}],
                        }
                    ],
                },
            )
        if path == f"/ws/2/release/{RELEASE}":
            return httpx.Response(200, json=release)
        if path.startswith("/ws/2/recording/"):
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "title": "Identified"})
        assert path == "/ws/2/release" and request.url.params["recording"] in {
            mbid(f"rec-{i}") for i in range(1, len(lengths) + 1)
        }
        return httpx.Response(200, json={"releases": [release], "release-count": 1})

    _install_transport(monkeypatch, handle)
    assert f"Import: The Album ({RELEASE})" in _invoke(config, "import", str(folder))
    with closing(open_db(tmp_path / "data" / "trawl.sqlite")) as conn:
        assert conn.execute("SELECT origin,origin_ref FROM wants").fetchone()[:] == (
            "manual",
            f"import:{folder}",
        )
        rows = list(
            conn.execute(
                """SELECT path,verified_by FROM tracks
                WHERE source='local' AND state='done' ORDER BY position"""
            )
        )
        assert len(rows) == 3
        for i, (raw, verified) in enumerate(rows, 1):
            assert verified == "acoustid" and FLAC(Path(raw))["musicbrainz_trackid"] == [
                mbid(f"rec-{i}")
            ]
    assert len(list(folder.glob("*.flac"))) == 3
