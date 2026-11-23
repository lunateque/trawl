import asyncio
import os
import signal
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tomllib import loads
from xml.etree.ElementTree import fromstring

import httpx
import pytest
from conftest import mbid

from trawl.core.catalog import ArtistRef, JsonObject
from trawl.db.connect import open_db
from trawl.mb.client import MusicBrainzClient
from trawl.net.client import HttpClient
from trawl.runner import service
from trawl.runner.watch import Job, graceful_stop, watch
from trawl.taste.oauth import replace_token, write_token
from trawl.taste.releases import save_artist, sync_releases
from trawl.taste.store import cursor


async def test_watch_order_failure_and_restart(tmp_path: Path) -> None:
    now = 1000.0
    events: list[str] = []
    names = ("taste", "releases", "plan", "run", "retry", "upgrade")
    sleeps: list[float] = []

    def clock() -> float:
        return now

    def make_job(name: str) -> Job:
        async def run() -> None:
            events.append(name)
            if name == "taste":
                raise ValueError("fixture job error")

        return Job(name, 60 if name in {"plan", "run", "retry"} else 3600, run)

    async def sleep(stop: asyncio.Event, delay: float) -> None:
        sleeps.append(delay)
        stop.set()

    jobs = tuple(map(make_job, names))
    path = tmp_path / "db.sqlite"
    with closing(open_db(path)) as conn:
        await watch(conn, jobs, asyncio.Event(), clock=clock, sleep=sleep)
        assert events == list(names)
        assert all(cursor(conn, f"watch:{name}") == "1000.0" for name in names)
        assert sleeps == [60]
    now += 59
    with closing(open_db(path)) as conn:
        await watch(conn, jobs, asyncio.Event(), clock=clock, sleep=sleep)
        assert events == list(names)
        assert sleeps[-1] == 1
        now += 1
        await watch(conn, jobs, asyncio.Event(), clock=clock, sleep=sleep)
        assert events == [*names, "plan", "run", "retry"]


async def test_watch_installs_and_restores_signal_handler() -> None:
    previous = signal.getsignal(signal.SIGINT)
    stop = asyncio.Event()
    with graceful_stop(stop):
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(stop.wait(), timeout=2)
    assert signal.getsignal(signal.SIGINT) is previous


async def test_watch_shutdown_completes_active_job(tmp_path: Path) -> None:
    stop = asyncio.Event()
    events: list[str] = []

    async def active() -> None:
        events.append("start")
        stop.set()
        await asyncio.sleep(0)
        events.append("finished")

    async def later() -> None:
        pytest.fail("no later job should start after stop")

    with closing(open_db(tmp_path / "db.sqlite")) as conn:
        await watch(
            conn, [Job("active", 10, active), Job("later", 10, later)], stop, clock=lambda: 42
        )
        assert events == ["start", "finished"]
        assert cursor(conn, "watch:active") == "42"
        assert cursor(conn, "watch:later") is None


def group(identifier: str, date: str, kind: str = "Album") -> JsonObject:
    return {
        "id": mbid(identifier),
        "title": identifier,
        "first-release-date": date,
        "primary-type": kind,
    }


async def test_releases_first_window_type_cursor_and_manual_unfollow(tmp_path: Path) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    groups = [
        group("old", "2026-01-01"),
        group("new", "2026-05-12"),
        group("ep", "2026-04-08", "EP"),
        group("single", "2026-05-12", "Single"),
    ]

    def handle(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/ws/2/release-group"
        assert req.url.params["artist"] == "followed"
        assert req.url.params["type"] == "album|ep"
        return httpx.Response(
            200, json={"release-group-count": len(groups), "release-groups": groups}
        )

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    try:
        with closing(open_db(tmp_path / "db.sqlite")) as conn:
            mb = MusicBrainzClient(http, conn, 30)
            followed = ArtistRef("followed", "Artist", "Artist", None)
            muted = ArtistRef("muted", "Other", "Other", None)
            with conn:
                save_artist(conn, followed, followed=True)
                save_artist(conn, muted, followed=False, manual=True)
                save_artist(conn, muted, followed=True)
            assert (await sync_releases(conn, mb, ("album", "ep"), now=now)).wants == 2
            assert {tuple(row) for row in conn.execute("SELECT mbid,origin FROM wants")} == {
                (mbid("new"), "releases"),
                (mbid("ep"), "releases"),
            }
            assert (
                conn.execute(
                    "SELECT releases_checked_at FROM artists WHERE mbid=?", ("followed",)
                ).fetchone()[0]
                == now.isoformat()
            )
            groups.extend((group("next", "2026-06-02"), group("not-after-cursor", "2026-06-01")))
            tomorrow = now + timedelta(days=1)
            assert (await sync_releases(conn, mb, ("album", "ep"), now=tomorrow)).wants == 1
            assert conn.execute("SELECT mbid FROM wants ORDER BY id DESC LIMIT 1").fetchone()[
                0
            ] == mbid("next")
            assert not mb.refresh
    finally:
        await http.aclose()


def test_oauth_writes_only_refresh_token(tmp_path: Path) -> None:
    path = tmp_path / "secrets.toml"
    original = '# keep\r\nspotify_client_id = "fixture"\r\nspotify_refresh_token = "old" # keep\r\n'
    path.write_bytes(original.encode())
    write_token(path, "new")
    assert path.read_bytes() == original.replace('"old"', '"new"').encode()
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600
    nested = "[unrelated]\nkey = 1\n"
    assert loads(replace_token(nested, "new")) == {
        "spotify_refresh_token": "new",
        "unrelated": {"key": 1},
    }


def test_service_templates_and_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "a config & more.toml"
    args = service.command(config)
    assert args[1:] == ("-m", "trawl", "--config", str(config), "run", "--watch")
    root = fromstring(service.windows_xml(args, "Domain\\User"))  # noqa: S314
    ns = {"s": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.findtext("s:Triggers/s:LogonTrigger/s:UserId", namespaces=ns) == "Domain\\User"
    assert root.findtext("s:Settings/s:Hidden", namespaces=ns) == "true"
    assert root.findtext("s:Settings/s:RestartOnFailure/s:Count", namespaces=ns) == "3"
    assert (
        root.findtext("s:Actions/s:Exec/s:Arguments", namespaces=ns)
        == f'-m trawl --config "{config}" run --watch'
    )
    unit = service.systemd_unit(args)
    assert "Restart=on-failure" in unit
    assert '"--config"' in unit
    assert '"--watch"' in unit
    calls: list[tuple[str, ...]] = []

    def call(binary: str, *params: str) -> int:
        calls.append((binary, *params))
        if binary == "schtasks" and params[0] == "/Create":
            assert "LogonTrigger" in Path(params[4]).read_text(encoding="utf-16")
        return 0

    monkeypatch.setattr(service, "_call", call)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert service._windows("install", args) == 0  # pyright: ignore[reportPrivateUsage]
    assert calls[-1][:5] == ("schtasks", "/Create", "/TN", "trawl", "/XML")
    assert service._windows("uninstall", args) == 0  # pyright: ignore[reportPrivateUsage]
    assert calls[-1] == ("schtasks", "/Delete", "/TN", "trawl", "/F")
    assert service._linux("install", args) == 0  # pyright: ignore[reportPrivateUsage]
    assert (tmp_path / "systemd" / "user" / "trawl.service").read_text(encoding="utf-8") == unit
    assert calls[-1] == ("systemctl", "--user", "enable", "--now", "trawl")
    assert service._linux("uninstall", args) == 0  # pyright: ignore[reportPrivateUsage]
    assert not (tmp_path / "systemd" / "user" / "trawl.service").exists()
