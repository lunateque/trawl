import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trawl.config import Config
from trawl.db import queue, repo, review
from trawl.db.states import TrackState


def explain_text(explanation: repo.Explanation) -> str:
    lines: list[str] = []
    for release in explanation.releases:
        reason = json.dumps(json.loads(release.choice_reason), ensure_ascii=False, indent=2)
        lines.extend((f"Release: {release.title} ({release.mbid})", "choice_reason:", reason))
    lines.append("Wants:")
    lines.extend(
        f"  #{want.id} {want.kind} | {want.origin} | {want.origin_ref} | {want.created_at}"
        for want in explanation.wants
    )
    lines.append("Attempts:")
    lines.extend(
        f"  Track {attempt.track_id} | {attempt.at} | {attempt.source} | {attempt.outcome}"
        f" | {attempt.candidate_ref or '-'} | score: {attempt.score} | {attempt.reason or '-'}"
        for attempt in explanation.attempts
    )
    if not explanation.attempts:
        lines.append("  None")
    return "\n".join(lines)


@dataclass(frozen=True)
class TrackRow:
    id: int
    state: TrackState
    title: str
    release: str
    recording: str
    duration_ms: int | None
    pin: str | None
    revision: int
    attempts: str


def revision(conn: sqlite3.Connection, track_id: int) -> int | None:
    return conn.execute(
        "SELECT max(id) FROM events WHERE track_id=? AND state IS NOT NULL", (track_id,)
    ).fetchone()[0]


@dataclass(frozen=True)
class SourceRow:
    name: str
    accepted: int
    rejected: int
    pause: queue.Pause | None


@dataclass(frozen=True)
class Event:
    id: str
    at: datetime
    label: str
    detail: str
    track_id: int | None


@dataclass(frozen=True)
class UnresolvedRow:
    id: int
    origin: str
    ref: str
    artist: str | None
    title: str | None
    album: str | None
    reason: str


@dataclass(frozen=True)
class Snapshot:
    counts: dict[str, int]
    sources: list[SourceRow]
    events: list[Event]
    tracks: dict[int, TrackRow]
    rejected: dict[int, review.Rejection | None]
    unresolved: dict[int, UnresolvedRow]


def _source_rows(conn: sqlite3.Connection, cfg: Config) -> list[SourceRow]:
    since = datetime.now(UTC) - timedelta(hours=1)
    outcomes: dict[str, tuple[int, int]] = {
        source: (ok, rejected)
        for source, ok, rejected in conn.execute(
            """SELECT source,sum(outcome='accepted'),sum(outcome!='accepted')
               FROM attempts WHERE at>=? GROUP BY source""",
            (since.isoformat(),),
        )
    }
    pauses = {pause.source: pause for pause in queue.pauses(conn)}
    names = dict.fromkeys((*cfg.sources.order, *sorted(outcomes.keys() | pauses.keys())))
    return [
        SourceRow(source, *outcomes.get(source, (0, 0)), pauses.get(source)) for source in names
    ]


def _events(conn: sqlite3.Connection) -> list[Event]:
    rows: list[Event] = []
    for row in conn.execute(
        """SELECT e.id,e.at,e.track_id,e.state,e.codec,e.verified_by,a.outcome,a.reason,
                  t.artist_credit,t.title,r.title AS release_title,a.source
           FROM (SELECT * FROM events ORDER BY at DESC,id DESC LIMIT 200) e
           JOIN tracks t ON t.id=e.track_id JOIN releases r ON r.mbid=t.release_mbid
           LEFT JOIN attempts a ON a.id=e.attempt_id ORDER BY e.at DESC,e.id DESC"""
    ):
        label = row["state"] or row["outcome"]
        extra = (
            ", ".join(part for part in (row["codec"], row["verified_by"]) if part)
            if row["state"]
            else ": ".join(part for part in (row["source"], row["reason"]) if part)
        )
        detail = f"{row['artist_credit']} — {row['release_title']} / {row['title']}"
        if extra:
            detail += f" ({extra})"
        rows.append(
            Event(str(row["id"]), datetime.fromisoformat(row["at"]), label, detail, row["track_id"])
        )
    return rows


def snapshot(conn: sqlite3.Connection, cfg: Config) -> Snapshot:
    counts: dict[str, int] = dict(conn.execute("SELECT state,count(*) FROM tracks GROUP BY state"))
    events = _events(conn)
    tracks: dict[int, TrackRow] = {}
    rejected: dict[int, review.Rejection | None] = {}
    for row in conn.execute(
        """SELECT t.id,t.state,t.title,r.title,t.recording_mbid,t.length_ms,t.pinned_candidate,
                  (SELECT max(e.id) FROM events e WHERE e.track_id=t.id AND e.state IS NOT NULL),
                  (SELECT group_concat(detail,'; ') FROM (
                      SELECT source || ': ' || outcome || COALESCE(' — ' || reason,'') AS detail
                      FROM attempts WHERE track_id=t.id ORDER BY id DESC LIMIT 2)),
                  EXISTS(SELECT 1 FROM current_rejections f WHERE f.track_id=t.id)
           FROM tracks t JOIN releases r ON r.mbid=t.release_mbid
           WHERE t.state='missing'
              OR (t.pinned_candidate IS NOT NULL AND t.state IN ('planned','searching'))
              OR t.id IN (SELECT track_id FROM events ORDER BY at DESC,id DESC LIMIT 200)
           ORDER BY t.id"""
    ):
        track = TrackRow(
            row[0],
            TrackState(row[1]),
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8] or "",
        )
        tracks[track.id] = track
        if row[9] and (rejection := review.rejection(conn, track.id, cfg.library_root)):
            rejected[track.id] = rejection
        elif track.pin and track.state in (
            TrackState.PLANNED,
            TrackState.SEARCHING,
            TrackState.MISSING,
        ):
            rejected[track.id] = None
    unresolved: dict[int, UnresolvedRow] = {
        row[0]: UnresolvedRow(*row)
        for row in conn.execute(
            "SELECT id,origin,origin_ref,artist,title,album,reason FROM unresolved ORDER BY id"
        )
    }
    return Snapshot(counts, _source_rows(conn, cfg), events, tracks, rejected, unresolved)
