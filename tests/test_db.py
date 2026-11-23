import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trawl import cli
from trawl.cli import app
from trawl.db import queue
from trawl.db.connect import open_db
from trawl.db.states import Actor, TrackState, transition
from trawl.runner.lock import acquire

MOVES: set[tuple[str, str, Actor]] = {
    (src, dst, Actor.RUNNER)
    for src, dst in (
        ("planned", "searching"),
        ("searching", "fetched"),
        ("fetched", "searching"),
        ("fetched", "verified"),
        ("verified", "tagged"),
        ("tagged", "done"),
        ("tagged", "tagged"),
        ("searching", "missing"),
        ("searching", "planned"),
        ("missing", "planned"),
        ("planned", "verified"),
        ("done", "done"),
    )
} | {
    (src, dst, Actor.USER)
    for src, dst in (
        ("missing", "planned"),
        ("missing", "verified"),
        ("skipped", "planned"),
        ("missing", "searching"),
        ("searching", "searching"),
        ("planned", "searching"),
        *((state.value, "skipped") for state in TrackState if state != TrackState.DONE),
    )
}


@pytest.fixture
def conn(tmp_path: Path):
    with closing(open_db(tmp_path / "data" / "trawl.sqlite")) as db:
        db.execute(
            """INSERT INTO releases
               (mbid,release_group_mbid,title,artist_credit,disc_count,choice_reason)
               VALUES ('r','rg','Release','Artist',1,'{}')"""
        )
        db.execute("""
            INSERT INTO tracks (id,release_mbid,release_track_mbid,recording_mbid,disc,
                                disc_track_count,position,title,artist_credit,artist_mbids,state)
            VALUES (1,'r','rt','rec',1,1,1,'Track','Artist','[]','planned')
        """)
        yield db


def test_migrations(tmp_path: Path) -> None:
    path = tmp_path / "new" / "test.sqlite"
    for _ in range(2):
        with closing(open_db(path)) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 5
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            assert (
                db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE "
                    "'sqlite_%'"
                ).fetchone()[0]
                == 16
            )


def test_transition_matrix(conn: sqlite3.Connection) -> None:
    for src in TrackState:
        for dst in TrackState:
            for actor in Actor:
                conn.execute("UPDATE tracks SET state = ? WHERE id = 1", (src,))
                if (src, dst, actor) in MOVES:
                    assert transition(conn, 1, src, dst, actor)
                    assert (
                        conn.execute("SELECT state FROM tracks WHERE id = 1").fetchone()[0] == dst
                    )
                else:
                    with pytest.raises(ValueError, match="transition"):
                        transition(conn, 1, src, dst, actor)


def test_invalid_and_stale_moves(conn: sqlite3.Connection) -> None:
    assert not transition(
        conn, 1, TrackState.FETCHED, TrackState.VERIFIED, Actor.RUNNER, path="wrong"
    )
    assert tuple(conn.execute("SELECT state, path FROM tracks").fetchone()) == ("planned", None)
    with pytest.raises(ValueError, match="unknown"):
        transition(conn, 1, TrackState.PLANNED, TrackState.SEARCHING, Actor.RUNNER, id=5)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE tracks SET state = 'invalid' WHERE id = 1")


def test_runner_cannot_overwrite_new_pin(conn: sqlite3.Connection) -> None:
    assert transition(
        conn,
        1,
        TrackState.PLANNED,
        TrackState.SEARCHING,
        Actor.USER,
        pinned_candidate="new",
    )
    assert not transition(
        conn,
        1,
        TrackState.SEARCHING,
        TrackState.FETCHED,
        Actor.RUNNER,
        expected_pin=None,
    )
    assert transition(
        conn,
        1,
        TrackState.SEARCHING,
        TrackState.FETCHED,
        Actor.RUNNER,
        expected_pin="new",
    )


def test_source_pause_backoff_resets_after_success(conn: sqlite3.Connection) -> None:
    for seconds in (900, 1800, 3600):
        first = queue.wall(conn, "ytmusic", "HTTP 429")
        assert queue.wall(conn, "ytmusic", "HTTP 403") == first
        row = conn.execute("SELECT backoff_s, reason FROM source_state").fetchone()
        assert tuple(row) == (seconds, "HTTP 429")
        conn.execute("UPDATE source_state SET paused_until='2000-01-01T00:00:00Z'")
    queue.source_succeeded(conn, "ytmusic")
    assert queue.pauses(conn) == []
    queue.wall(conn, "ytmusic", "HTTP 403")
    assert conn.execute("SELECT backoff_s FROM source_state").fetchone()[0] == 900


def test_pick_persists_while_another_runner_holds_lock(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn.commit()
    data = tmp_path / "data"
    config = tmp_path / "config.toml"
    config.write_text(f'contact_email="a@b"\ndata_dir="{data.as_posix()}"\n', encoding="utf-8")
    (tmp_path / "secrets.toml").write_text('acoustid_key="fixture"\n', encoding="utf-8")

    async def no_runner(*args: object, **kwargs: object) -> None:
        pytest.fail("pick must not launch a second runner")

    monkeypatch.setattr(cli, "_process", no_runner)
    with acquire(data):
        result = CliRunner().invoke(
            app, ["--config", str(config), "pick", "1", "https://youtu.be/a1b2c3d4e5f"]
        )
    assert result.exit_code == 0, result.output
    assert "Pinned track 1; another trawl is running" in result.output
    work = queue.pending(conn)
    assert work[0].state == TrackState.SEARCHING
    assert work[0].pin is not None and "a1b2c3d4e5f" in work[0].pin
