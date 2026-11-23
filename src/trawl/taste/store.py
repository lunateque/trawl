import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from trawl.config import Config
from trawl.db import queue, repo
from trawl.db.repo import Kind, Origin, Scope
from trawl.mb.client import MusicBrainzClient
from trawl.plan.choose import choose_recording_group
from trawl.plan.wants import MbLink
from trawl.resolve.match import ExternalTrack, Resolved, Unresolved, resolve


@dataclass
class SyncReport:
    wants: int = 0
    unresolved: int = 0
    message: str = ""


@dataclass(frozen=True)
class Like:
    track: ExternalTrack
    at: str


DEFAULT_SCOPE = {
    Kind.ARTIST: Scope.DISCOGRAPHY,
    Kind.RECORDING: Scope.TRACK,
    Kind.RELEASE: Scope.ALBUM,
    Kind.RELEASE_GROUP: Scope.ALBUM,
}


def cursor(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM cursors WHERE origin=?", (key,)).fetchone()
    return row[0] if row else None


def set_cursor(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO cursors(origin,value,updated_at) VALUES (?,?,?)
        ON CONFLICT(origin) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (key, value, queue.now()),
    )


def moment(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def advance(conn: sqlite3.Connection, key: str, likes: Iterable[Like]) -> None:
    newest = max((moment(like.at) for like in likes), default=None)
    if newest is not None:
        with conn:
            previous = cursor(conn, key)
            if previous is None or newest > moment(previous):
                set_cursor(conn, key, newest.isoformat())


def resolved(conn: sqlite3.Connection, origin: Origin, ref: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM resolved_refs WHERE origin=? AND origin_ref=?", (origin, ref)
        ).fetchone()
        is not None
    )


def mark_resolved(conn: sqlite3.Connection, origin: Origin, ref: str) -> None:
    conn.execute("INSERT OR IGNORE INTO resolved_refs VALUES (?,?)", (origin, ref))
    conn.execute("DELETE FROM unresolved WHERE origin=? AND origin_ref=?", (origin, ref))


def record_unresolved(conn: sqlite3.Connection, row: ExternalTrack, reason: str) -> None:
    conn.execute(
        """INSERT INTO unresolved (
            origin,origin_ref,artist,title,album,length_ms,isrc,upc,reason,last_try_at,target_kind
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(origin,origin_ref) DO UPDATE SET
            artist=excluded.artist,title=excluded.title,album=excluded.album,
            length_ms=excluded.length_ms,isrc=excluded.isrc,upc=excluded.upc,
            reason=excluded.reason,last_try_at=excluded.last_try_at,target_kind=excluded.target_kind""",
        (
            row.origin,
            row.origin_ref,
            row.artist,
            row.title,
            row.album,
            row.length_ms,
            row.isrc,
            row.upc,
            reason,
            queue.now(),
            row.kind,
        ),
    )


def unresolved_tracks(conn: sqlite3.Connection, origin: Origin) -> list[ExternalTrack]:
    return [
        ExternalTrack(origin, ref, artist or "", title or "", album, length, isrc, upc, Kind(kind))
        for ref, artist, title, album, length, isrc, upc, kind in conn.execute(
            """SELECT origin_ref,artist,title,album,length_ms,isrc,upc,target_kind
            FROM unresolved WHERE origin=? ORDER BY id""",
            (origin,),
        )
    ]


def bind(
    conn: sqlite3.Connection,
    unresolved_id: int,
    target: MbLink,
    artist_limit: int,
    *,
    expected: tuple[str, str] | None = None,
) -> int | None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT origin,origin_ref FROM unresolved WHERE id=?", (unresolved_id,)
        ).fetchone()
        if row is None or (expected is not None and tuple(row) != expected):
            return None
        origin, ref = row
        want = repo.insert_want(
            conn,
            target.kind,
            target.mbid,
            DEFAULT_SCOPE[target.kind],
            Origin(origin),
            ref,
            artist_limit if target.kind == Kind.ARTIST else None,
            preserve_origin=True,
        )
        mark_resolved(conn, Origin(origin), ref)
        return want


async def ingest(
    conn: sqlite3.Connection,
    mb: MusicBrainzClient,
    cfg: Config,
    track: ExternalTrack,
    report: SyncReport,
    *,
    known_mbid: str | None = None,
    scope: Scope | None = None,
    limit: int | None = None,
) -> None:
    if resolved(conn, track.origin, track.origin_ref):
        return
    result = Resolved(known_mbid, track.kind) if known_mbid else await resolve(mb, track)
    album_scope = scope == Scope.ALBUM or (
        scope is None
        and track.origin in {Origin.SPOTIFY, Origin.YANDEX}
        and track.origin_ref.startswith("track:")
        and cfg.likes_scope == "album"
    )
    if isinstance(result, Resolved) and result.kind == Kind.RECORDING and album_scope:
        group = choose_recording_group(await mb.recording_releases(result.mbid))
        result = (
            Resolved(group.mbid, Kind.RELEASE_GROUP)
            if group
            else Unresolved("Recording has no eligible release group")
        )
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if resolved(conn, track.origin, track.origin_ref):
            return
        if isinstance(result, Unresolved):
            record_unresolved(conn, track, result.reason)
            report.unresolved += 1
        else:
            if result.kind == Kind.ARTIST and limit is None:
                limit = cfg.plan.discography_limit
            before = conn.total_changes
            repo.insert_want(
                conn,
                result.kind,
                result.mbid,
                scope or DEFAULT_SCOPE[result.kind],
                track.origin,
                track.origin_ref,
                limit,
                preserve_origin=True,
            )
            report.wants += conn.total_changes != before
            mark_resolved(conn, track.origin, track.origin_ref)


async def ingest_collection(
    conn: sqlite3.Connection,
    mb: MusicBrainzClient,
    cfg: Config,
    origin: Origin,
    tracks: Iterable[ExternalTrack],
    report: SyncReport,
) -> None:
    items = {track.origin_ref: track for track in unresolved_tracks(conn, origin)}
    items.update((track.origin_ref, track) for track in tracks)
    for item in items.values():
        await ingest(conn, mb, cfg, item, report)
