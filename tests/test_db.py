import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from trawl.db.connect import open_db
from trawl.db.states import Actor, TrackState, transition

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
            "INSERT INTO releases VALUES ('r','rg','Release','Artist',NULL,NULL,NULL,NULL,1,'{}')"
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
            assert db.execute("PRAGMA user_version").fetchone()[0] == 1
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            assert (
                db.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
                == 10
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
