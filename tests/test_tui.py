import asyncio
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from fixtures.seed_tui import seed
from textual.widgets import Input, Static, TabbedContent
from typer.testing import CliRunner

from trawl.cli import app
from trawl.db import queue
from trawl.db.connect import open_db
from trawl.runner.lock import LockBusy, acquire
from trawl.runner.progress import Progress
from trawl.tui.app import TrawlApp
from trawl.tui.dialogs import Grid


def state(db: sqlite3.Connection, track: int) -> tuple[str, str | None, str | None]:
    row = db.execute(
        "SELECT state,pinned_candidate,verified_by FROM tracks WHERE id=?", (track,)
    ).fetchone()
    return row[0], row[1], row[2]


def test_cli_exposes_tui() -> None:
    result = CliRunner().invoke(app, ["tui", "--help"])
    assert result.exit_code == 0, result.output


async def test_observer_tabs_and_manual_decisions(tmp_path: Path) -> None:
    cfg = seed(tmp_path)
    with acquire(cfg.data_dir), closing(open_db(cfg.data_dir / "trawl.sqlite")) as db:
        app = TrawlApp(cfg, db)
        async with app.run_test(size=(110, 37)) as pilot:
            assert f"observer (pid {os.getpid()})" in str(app.query_one("#status", Static).content)
            summary = str(app.query_one("#counts", Static).content)
            for label in ("planned", "searching", "fetched", "verified", "tagged", "done"):
                assert label in summary
            assert "unresolved" in summary
            assert app.query_one("#missing", Grid).row_count == 3
            await pilot.press("e")
            assert "Release: Demo Album" in str(app.screen.query_one("#detail", Static).content)
            await pilot.press("escape", "p")
            assert app.screen.query_one("#candidates", Grid).row_count == 2
            await pilot.press("enter")
            assert state(db, 7)[0] == "searching"
            assert "forest-peer" in (state(db, 7)[1] or "")

            await pilot.press("2", "a")
            assert "comparison-hit" in str(app.screen.query_one("#detail", Static).content)
            await pilot.press("enter")
            assert state(db, 9) == ("verified", None, "manual")
            staged = Path(db.execute("SELECT path FROM tracks WHERE id=9").fetchone()[0])
            assert await asyncio.to_thread(staged.is_file)

            await pilot.press("3", "l")
            assert app.screen.query_one(Input).has_focus
            app.screen.query_one(
                Input
            ).value = "https://musicbrainz.org/recording/abcde123-abcd-abcd-abcd-abcde123abcd"
            await pilot.press("enter")
            assert db.execute("SELECT count(*) FROM unresolved").fetchone()[0] == 0
            assert db.execute("SELECT origin_ref FROM wants").fetchone()[0] == "track:777"

            await pilot.press("1")
            assert app.query_one(TabbedContent).active == "tab-missing"
            with db:
                assert queue.skip(db, 10) == 1
            await pilot.press("r")
            assert state(db, 10)[0] == "skipped"
            notes = app._notifications  # pyright: ignore[reportPrivateUsage]
            assert any(note.message == "already changed by the runner" for note in notes)
            with db:
                queue.retry(db, skipped=True)
                db.execute("UPDATE tracks SET state='missing' WHERE id=10")
            app.refresh_dashboard()
            await pilot.press("r")
            assert state(db, 10)[0] == "planned"
            events = app.query_one("#events", Grid)
            events.focus()
            cursor = next(i for i, row in enumerate(app.snapshot.events) if row.track_id == 10)
            events.move_cursor(row=cursor)
            await pilot.press("s")
            assert state(db, 10)[0] == "skipped"


async def test_paste_previews_and_refuses_a_stale_selection(tmp_path: Path) -> None:
    cfg = seed(tmp_path)
    local = tmp_path / "recording.flac"
    await asyncio.to_thread(local.write_bytes, b"audio")
    with acquire(cfg.data_dir), closing(open_db(cfg.data_dir / "trawl.sqlite")) as db:
        app = TrawlApp(cfg, db)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.press("p", "u")
            app.screen.query_one(Input).value = str(local)
            await pilot.press("enter")
            assert queue.Pin.load(state(db, 7)[1] or "") == queue.Pin("path", str(local))

            await pilot.press("p", "u")
            app.screen.query_one(Input).value = "https://music.youtube.com/watch?v=a1b2c3d4e5f"
            with db:
                assert queue.pin(db, 9, "path", str(local))
                db.execute("UPDATE tracks SET state='missing' WHERE id=9")
            await pilot.press("enter")
            assert queue.Pin.load(state(db, 9)[1] or "") == queue.Pin("path", str(local))


async def test_embedded_watch_toggles_and_quits_after_current_transition(tmp_path: Path) -> None:
    cfg = seed(tmp_path)
    started, finished = asyncio.Event(), asyncio.Event()
    invocations = 0

    async def watch(stop: asyncio.Event, progress: Progress) -> None:
        nonlocal invocations
        invocations += 1
        with progress.downloading("ytmusic", "First"), progress.downloading("ytmusic", "Second"):
            started.set()
            await stop.wait()
            await asyncio.sleep(0.01)
        finished.set()

    with closing(open_db(cfg.data_dir / "trawl.sqlite")) as db:
        app = TrawlApp(cfg, db, watch)
        async with app.run_test(size=(100, 35)) as pilot:
            await asyncio.wait_for(started.wait(), 1)
            try:
                with acquire(cfg.data_dir):
                    raise AssertionError("embedded runner didn't hold the lock")
            except LockBusy:
                pass
            app.refresh_dashboard()
            assert "2/2 downloading" in str(app.query_one("#sources", Static).content)
            await pilot.press("w")
            await asyncio.wait_for(finished.wait(), 1)
            await pilot.pause()
            assert "stopped" in str(app.query_one("#status", Static).content)
            started.clear()
            finished.clear()
            await pilot.press("w")
            await asyncio.wait_for(started.wait(), 1)
            await pilot.press("ctrl+c")
        assert invocations == 2 and finished.is_set()
        with acquire(cfg.data_dir):
            pass
