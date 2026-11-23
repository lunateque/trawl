import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from trawl.mb.models import Medium, Release, Track, credit_string


class Kind(StrEnum):
    ARTIST = "artist"
    RELEASE_GROUP = "release_group"
    RELEASE = "release"
    RECORDING = "recording"


class Scope(StrEnum):
    TRACK = "track"
    ALBUM = "album"
    DISCOGRAPHY = "discography"


class Origin(StrEnum):
    LISTENBRAINZ = "listenbrainz"
    YANDEX = "yandex"
    SPOTIFY = "spotify"
    MANUAL = "manual"
    RELEASES = "releases"


class WantStatus(StrEnum):
    NEW = "new"
    PLANNED = "planned"
    EMPTY = "empty"
    DROPPED = "dropped"


@dataclass(frozen=True)
class Want:
    id: int
    kind: Kind
    mbid: str
    scope: Scope
    origin: Origin
    limit: int | None


def insert_want(
    conn: sqlite3.Connection,
    kind: Kind,
    mbid: str,
    scope: Scope,
    origin: Origin,
    origin_ref: str,
    limit: int | None,
) -> int:
    row = conn.execute(
        """INSERT INTO wants (kind, mbid, scope, origin, origin_ref, "limit", status, created_at)
        SELECT ?, ?, ?, ?, ?, ?, 'new', ?
        WHERE NOT EXISTS (
            SELECT 1 FROM wants WHERE kind = ? AND mbid = ? AND scope = ? AND status != 'dropped'
        ) RETURNING id""",
        (
            kind,
            mbid,
            scope,
            origin,
            origin_ref,
            limit,
            datetime.now(UTC).isoformat(),
            kind,
            mbid,
            scope,
        ),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """SELECT id FROM wants
            WHERE kind = ? AND mbid = ? AND scope = ? AND status != 'dropped'
            ORDER BY id LIMIT 1""",
            (kind, mbid, scope),
        ).fetchone()
    return row["id"]


def next_want(conn: sqlite3.Connection, after: int) -> Want | None:
    row = conn.execute(
        """SELECT id, kind, mbid, scope, origin, "limit" FROM wants
        WHERE id > ? AND status = 'new' ORDER BY id LIMIT 1""",
        (after,),
    ).fetchone()
    if row is None:
        return None
    return Want(
        row["id"],
        Kind(row["kind"]),
        row["mbid"],
        Scope(row["scope"]),
        Origin(row["origin"]),
        row["limit"],
    )


def set_want_status(conn: sqlite3.Connection, want_id: int, status: WantStatus) -> None:
    conn.execute("UPDATE wants SET status = ? WHERE id = ? AND status = 'new'", (status, want_id))


def track_for_recording(conn: sqlite3.Connection, mbid: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM tracks WHERE recording_mbid = ? ORDER BY id LIMIT 1", (mbid,)
    ).fetchone()
    return None if row is None else row["id"]


def link_track(conn: sqlite3.Connection, want_id: int, track_id: int) -> None:
    conn.execute(
        "INSERT INTO want_tracks (want_id, track_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (want_id, track_id),
    )


def upsert_release(conn: sqlite3.Connection, release: Release, reason: dict[str, object]) -> None:
    group = release.release_group
    conn.execute(
        """INSERT INTO releases (mbid, release_group_mbid, title, artist_credit, date,
                                 original_date, type, status, disc_count, choice_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (mbid) DO UPDATE SET release_group_mbid = excluded.release_group_mbid,
            title = excluded.title, artist_credit = excluded.artist_credit, date = excluded.date,
            original_date = excluded.original_date, type = excluded.type, status = excluded.status,
            disc_count = excluded.disc_count,
            choice_reason = CASE
                WHEN json_extract(releases.choice_reason, '$.switched_from') IS NOT NULL
                     AND json_extract(excluded.choice_reason, '$.switched_from') IS NULL
                THEN json_patch(excluded.choice_reason, json_object(
                    'switched_from', json_extract(releases.choice_reason, '$.switched_from'),
                    'switched_reason', json_extract(releases.choice_reason, '$.switched_reason')
                ))
                ELSE excluded.choice_reason
            END""",
        (
            release.mbid,
            group.mbid,
            release.title,
            credit_string(release.credits),
            release.date,
            group.first_release_date,
            group.primary_type,
            release.status,
            len(release.media),
            json.dumps(reason, ensure_ascii=False),
        ),
    )


def insert_track(
    conn: sqlite3.Connection,
    release: Release,
    medium: Medium,
    disc_count: int,
    track: Track,
) -> tuple[int, bool]:
    credits = track.credits or release.credits
    inserted = conn.execute(
        """INSERT INTO tracks (release_mbid, release_track_mbid, recording_mbid, disc,
                              disc_track_count, position, title, artist_credit, artist_mbids,
                              length_ms, isrc, state)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned')
        ON CONFLICT (release_track_mbid) DO NOTHING RETURNING id""",
        (
            release.mbid,
            track.mbid,
            track.recording_mbid,
            medium.position,
            disc_count,
            track.position,
            track.title,
            credit_string(credits),
            json.dumps([credit.artist_mbid for credit in credits]),
            track.length_ms,
            track.isrcs[0] if track.isrcs else None,
        ),
    ).fetchone()
    if inserted is not None:
        return inserted["id"], True
    found = conn.execute(
        "SELECT id FROM tracks WHERE release_track_mbid = ?", (track.mbid,)
    ).fetchone()
    return found["id"], False


@dataclass(frozen=True)
class Status:
    tracks: list[tuple[str, int]]
    wants: list[tuple[str, str, int]]
    unresolved: int


def status(conn: sqlite3.Connection) -> Status:
    return Status(
        [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT state, count(*) FROM tracks GROUP BY state ORDER BY state",
            )
        ],
        [
            (row[0], row[1], row[2])
            for row in conn.execute(
                """SELECT status, origin, count(*) FROM wants
                GROUP BY status, origin ORDER BY status, origin""",
            )
        ],
        conn.execute("SELECT count(*) FROM unresolved").fetchone()[0],
    )


@dataclass(frozen=True)
class ReleaseDecision:
    mbid: str
    title: str
    choice_reason: str


@dataclass(frozen=True)
class WantSource:
    id: int
    kind: str
    origin: str
    origin_ref: str
    created_at: str


@dataclass(frozen=True)
class Attempt:
    track_id: int
    source: str
    candidate_ref: str | None
    score: float | None
    outcome: str
    reason: str | None
    at: str


@dataclass(frozen=True)
class Explanation:
    releases: list[ReleaseDecision]
    wants: list[WantSource]
    attempts: list[Attempt]


def explain(conn: sqlite3.Connection, kind: Kind | None, target: int | str) -> Explanation:
    match = {
        None: "t.id = ?",
        Kind.RELEASE: """r.mbid IN (
            WITH RECURSIVE editions(mbid) AS (
                SELECT ? UNION SELECT switched_to FROM releases JOIN editions USING (mbid)
                WHERE switched_to IS NOT NULL
            ) SELECT mbid FROM editions
        )""",
        Kind.RELEASE_GROUP: "r.release_group_mbid = ?",
        Kind.RECORDING: "t.recording_mbid = ?",
        Kind.ARTIST: """EXISTS (SELECT 1 FROM json_each(t.artist_mbids) WHERE value = ?1)
            OR EXISTS (SELECT 1 FROM want_tracks wt JOIN wants w ON w.id = wt.want_id
                       WHERE wt.track_id = t.id AND w.origin_ref = 'artist:' || ?1)""",
    }[kind]
    matched = conn.execute(
        f"SELECT t.id FROM tracks t JOIN releases r ON r.mbid = t.release_mbid WHERE {match}",  # noqa: S608
        (target,),
    )
    ids = (json.dumps([row["id"] for row in matched]),)
    releases = conn.execute(
        """SELECT DISTINCT r.mbid, r.title, r.choice_reason FROM releases r
        JOIN tracks t ON r.mbid = t.release_mbid WHERE t.id IN (SELECT value FROM json_each(?))
        ORDER BY r.title, r.mbid""",
        ids,
    )
    wants = conn.execute(
        """SELECT DISTINCT w.id, w.kind, w.origin, w.origin_ref, w.created_at FROM wants w
        JOIN want_tracks wt ON w.id = wt.want_id
        WHERE wt.track_id IN (SELECT value FROM json_each(?))
        ORDER BY w.id""",
        ids,
    )
    attempts = conn.execute(
        """SELECT track_id, source, candidate_ref, score, outcome, reason, at FROM attempts
        WHERE track_id IN (SELECT value FROM json_each(?)) ORDER BY at, id""",
        ids,
    )
    return Explanation(
        [ReleaseDecision(row[0], row[1], row[2]) for row in releases],
        [WantSource(row[0], row[1], row[2], row[3], row[4]) for row in wants],
        [Attempt(row[0], row[1], row[2], row[3], row[4], row[5], row[6]) for row in attempts],
    )
