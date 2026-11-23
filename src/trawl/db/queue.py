import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from trawl.db.states import Actor, TrackState, transition


def now() -> str:
    return datetime.now(UTC).isoformat()


def after(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


@dataclass(frozen=True)
class Work:
    id: int
    release_mbid: str
    track_mbid: str
    recording_mbid: str
    title: str
    length_ms: int | None
    path: Path | None
    state: TrackState
    pin: str | None
    album_scope: bool
    next_try_at: str | None


def _work(row: sqlite3.Row) -> Work:
    return Work(
        row["id"],
        row["release_mbid"],
        row["release_track_mbid"],
        row["recording_mbid"],
        row["title"],
        row["length_ms"],
        Path(row["path"]) if row["path"] else None,
        TrackState(row["state"]),
        row["pinned_candidate"],
        bool(row["album_scope"]),
        row["next_try_at"],
    )


def pending(
    conn: sqlite3.Connection,
    only: int | None = None,
    *,
    release_mbid: str | None = None,
) -> list[Work]:
    return [
        _work(row)
        for row in conn.execute(
            """SELECT t.*, NOT EXISTS (
                SELECT 1 FROM want_tracks WHERE track_id=t.id
            ) OR EXISTS (
                SELECT 1 FROM want_tracks wt JOIN wants w ON w.id=wt.want_id
                WHERE wt.track_id=t.id AND w.scope != 'track' AND w.status != 'dropped'
            ) AS album_scope
            FROM tracks t WHERE t.state IN ('planned','searching','fetched','verified','tagged')
                AND (t.next_try_at IS NULL OR t.next_try_at <= ?)
                AND (? IS NULL OR t.id=?) AND (? IS NULL OR t.release_mbid=?)
            ORDER BY t.release_mbid, t.disc, t.position, t.id""",
            (now(), only, only, release_mbid, release_mbid),
        )
    ]


@dataclass(frozen=True)
class Existing:
    path: Path
    source: str | None
    codec: str | None
    bitrate: int | None
    lossless: int | None


def reusable(conn: sqlite3.Connection, recording_mbid: str) -> Existing | None:
    for path, source, codec, bitrate, lossless in conn.execute(
        """SELECT path, source, codec, bitrate, lossless FROM tracks
        WHERE recording_mbid=? AND state='done' AND path IS NOT NULL ORDER BY lossless DESC, id""",
        (recording_mbid,),
    ):
        if (file := Path(path)).is_file():
            return Existing(file, source, codec, bitrate, lossless)
    return None


type Outcome = Literal[
    "accepted", "rejected_verify", "rejected_probe", "fetch_failed", "no_candidates"
]


def attempt(
    conn: sqlite3.Connection,
    track: int,
    source: str,
    ref: str | None,
    score: float | None,
    outcome: Outcome,
    reason: str,
) -> None:
    conn.execute(
        """INSERT INTO attempts(track_id,source,candidate_ref,score,outcome,reason,at)
        VALUES(?,?,?,?,?,?,?)""",
        (track, source, ref, score, outcome, reason, now()),
    )


@dataclass(frozen=True)
class Pause:
    source: str
    until: str
    reason: str


def pauses(conn: sqlite3.Connection) -> list[Pause]:
    return [
        Pause(*row)
        for row in conn.execute(
            "SELECT source, paused_until, reason FROM source_state "
            "WHERE paused_until > ? ORDER BY source",
            (now(),),
        )
    ]


def pause_until(conn: sqlite3.Connection, source: str) -> str | None:
    row = conn.execute(
        "SELECT paused_until FROM source_state WHERE source=? AND paused_until > ?", (source, now())
    ).fetchone()
    return row[0] if row else None


def wall(conn: sqlite3.Connection, source: str, reason: str) -> str:
    if existing := pause_until(conn, source):
        return existing
    row = conn.execute("SELECT backoff_s FROM source_state WHERE source=?", (source,)).fetchone()
    seconds = min(row[0] * 2, 86400) if row else 900
    until = after(seconds)
    conn.execute(
        """INSERT INTO source_state(source, paused_until, backoff_s, reason) VALUES(?,?,?,?)
        ON CONFLICT(source) DO UPDATE SET paused_until=excluded.paused_until,
        backoff_s=excluded.backoff_s, reason=excluded.reason""",
        (source, until, seconds, reason),
    )
    return until


def source_succeeded(conn: sqlite3.Connection, source: str) -> None:
    conn.execute("DELETE FROM source_state WHERE source=? AND paused_until <= ?", (source, now()))


def attempt_counts(conn: sqlite3.Connection, since: int = 0) -> list[tuple[str, int, int]]:
    return [
        tuple(row)
        for row in conn.execute(
            """SELECT source, sum(outcome='accepted'), sum(outcome!='accepted')
        FROM attempts WHERE id > ? GROUP BY source ORDER BY source""",
            (since,),
        )
    ]


def retry(conn: sqlite3.Connection, *, skipped: bool, due: str | None = None) -> int:
    changed = 0
    for track_id, state in conn.execute(
        """SELECT id,state FROM tracks WHERE
        (state='missing' AND (? IS NULL OR next_try_at <= ?))
        OR (? AND state='skipped')""",
        (due, due, skipped),
    ).fetchall():
        changed += transition(
            conn,
            track_id,
            TrackState(state),
            TrackState.PLANNED,
            Actor.USER if due is None else Actor.RUNNER,
            path=None,
            pinned_candidate=None,
            next_try_at=None,
        )
    return changed


def skip(conn: sqlite3.Connection, target: str | int) -> int:
    if isinstance(target, int):
        rows = conn.execute("SELECT id,state FROM tracks WHERE id=? AND state!='done'", (target,))
    else:
        rows = conn.execute(
            "SELECT id,state FROM tracks WHERE release_mbid=? AND state!='done'", (target,)
        )
    return sum(
        transition(
            conn,
            track_id,
            TrackState(state),
            TrackState.SKIPPED,
            Actor.USER,
            path=None,
            pinned_candidate=None,
            next_try_at=None,
        )
        for track_id, state in rows.fetchall()
    )


def pin(conn: sqlite3.Connection, track_id: int, source: str, ref: str) -> bool:
    row = conn.execute(
        "SELECT state FROM tracks WHERE id=? AND state IN ('missing','searching','planned')",
        (track_id,),
    ).fetchone()
    return row is not None and transition(
        conn,
        track_id,
        TrackState(row[0]),
        TrackState.SEARCHING,
        Actor.USER,
        pinned_candidate=json.dumps({"source": source, "ref": ref}),
        next_try_at=None,
    )


def done_paths(conn: sqlite3.Connection, release_mbid: str) -> list[Path]:
    return [
        Path(row[0])
        for row in conn.execute(
            "SELECT path FROM tracks WHERE release_mbid=? AND state='done' ORDER BY disc, position",
            (release_mbid,),
        )
        if row[0]
    ]


def gain_releases(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            """SELECT r.mbid FROM releases r WHERE album_gain_done=0
        AND (album_gain_next_try_at IS NULL OR album_gain_next_try_at <= ?)
        AND EXISTS (SELECT 1 FROM tracks WHERE release_mbid=r.mbid AND state='done')
        AND NOT EXISTS (SELECT 1 FROM tracks WHERE release_mbid=r.mbid AND state!='done')""",
            (now(),),
        )
    ]


def set_gain(conn: sqlite3.Connection, release_mbid: str, retry_at: str | None) -> None:
    conn.execute(
        "UPDATE releases SET album_gain_done=?, album_gain_next_try_at=? WHERE mbid=?",
        (retry_at is None, retry_at, release_mbid),
    )
