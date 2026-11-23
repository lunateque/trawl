import sqlite3
from dataclasses import dataclass
from pathlib import Path

from trawl.core.fetch import Candidate, CandidateItem, Fetched
from trawl.core.paths import linked_ancestor
from trawl.core.youtube import video_id
from trawl.db import queue
from trawl.db.states import Actor, TrackState, transition


def parse_pin(raw: str, sources: tuple[str, ...]) -> queue.Pin:
    if "://" in raw:
        ref = video_id(raw)
        if "ytmusic" not in sources:
            raise ValueError("Enable ytmusic in sources.order to pick a YouTube video")
        return queue.Pin("ytmusic", ref)
    path = Path(raw).resolve()
    if not path.is_file():
        raise ValueError(f"No audio file at {path}")
    return queue.Pin("path", str(path))


@dataclass(frozen=True)
class Choice:
    source: str
    ref: str
    item_ref: str
    size: int | None
    title: str
    score: float
    reason: str | None


def clear_candidates(conn: sqlite3.Connection, track_id: int) -> None:
    conn.execute("DELETE FROM search_candidates WHERE track_id=?", (track_id,))


def remember(
    conn: sqlite3.Connection, track_id: int, candidate: Candidate, item: CandidateItem
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO search_candidates
        (track_id,source,ref,item_ref,size,title,score,after_attempt)
        VALUES(?,?,?,?,?,?,?,(SELECT COALESCE(max(id),0) FROM attempts WHERE track_id=?))""",
        (
            track_id,
            candidate.source,
            candidate.ref,
            item.ref,
            item.size,
            item.title,
            candidate.score,
            track_id,
        ),
    )


def choices(conn: sqlite3.Connection, track_id: int) -> list[Choice]:
    return [
        Choice(*row)
        for row in conn.execute(
            """SELECT c.source,c.ref,c.item_ref,c.size,c.title,c.score,
               (SELECT a.reason FROM attempts a WHERE a.track_id=c.track_id
                   AND a.source=c.source AND a.candidate_key=c.ref AND a.candidate_ref=c.item_ref
                   AND a.id>c.after_attempt ORDER BY a.id DESC LIMIT 1)
               FROM search_candidates c WHERE c.track_id=?
               ORDER BY c.score DESC,c.source,c.ref,c.item_ref""",
            (track_id,),
        )
    ]


@dataclass(frozen=True)
class Rejection:
    attempt_id: int
    source: str
    reason: str
    path: Path
    codec: str
    bitrate: int | None
    lossless: int
    comparison: str


def reject(
    conn: sqlite3.Connection,
    track_id: int,
    source: str,
    candidate_key: str,
    score: float,
    reason: str,
    file: Fetched,
    comparison: str,
) -> None:
    attempt_id = queue.attempt(
        conn,
        track_id,
        source,
        file.item_ref,
        score,
        "rejected_verify",
        reason,
        candidate_key=candidate_key,
    )
    conn.execute(
        "INSERT INTO review_files VALUES(?,?,?,?,?,?)",
        (attempt_id, str(file.path), file.codec, file.bitrate, file.lossless, comparison),
    )


def rejection(conn: sqlite3.Connection, track_id: int, library_root: Path) -> Rejection | None:
    row = conn.execute(
        """SELECT attempt_id,r.source,reason,r.path,r.codec,r.bitrate,r.lossless,comparison
           FROM current_rejections r JOIN tracks t ON t.id=r.track_id
           WHERE t.id=? AND t.state='missing'""",
        (track_id,),
    ).fetchone()
    if row is None:
        return None
    path = Path(row[3]).absolute()
    staging = library_root.resolve() / ".trawl-staging"
    if linked_ancestor(path) or not path.resolve().is_relative_to(staging) or not path.is_file():
        return None
    return Rejection(row[0], row[1], row[2], path, row[4], row[5], row[6], row[7])


def accept(conn: sqlite3.Connection, track_id: int, shown: Rejection, library_root: Path) -> bool:
    if rejection(conn, track_id, library_root) != shown:
        return False
    return transition(
        conn,
        track_id,
        TrackState.MISSING,
        TrackState.VERIFIED,
        Actor.USER,
        path=str(shown.path),
        source=shown.source,
        codec=shown.codec,
        bitrate=shown.bitrate,
        lossless=shown.lossless,
        verified_by="manual",
        pinned_candidate=None,
        next_try_at=None,
    )
