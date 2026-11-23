import sqlite3
from collections import defaultdict
from collections.abc import Collection

from trawl.config.models import PlanConfig
from trawl.core.catalog import Release, release_tracks
from trawl.db import repo
from trawl.db.states import Actor, TrackState, transition
from trawl.plan.choose import choose_release


def can_switch(conn: sqlite3.Connection, release: Release) -> bool:
    return (
        conn.execute(
            """SELECT 1 FROM tracks t JOIN releases r ON r.mbid=t.release_mbid
            WHERE (r.release_group_mbid=? AND t.state='done') OR
                  (r.mbid=? AND (t.state IN ('fetched','verified','tagged')
                               OR t.pinned_candidate IS NOT NULL))
            LIMIT 1""",
            (release.release_group.mbid, release.mbid),
        ).fetchone()
        is None
    )


def _want_links(
    conn: sqlite3.Connection, old_mbid: str, recordings: set[str]
) -> tuple[bool, set[int], dict[str, set[int]]] | None:
    wants = conn.execute(
        """SELECT DISTINCT w.id, w.scope, t.recording_mbid FROM wants w
        JOIN want_tracks wt ON wt.want_id=w.id JOIN tracks t ON t.id=wt.track_id
        WHERE t.release_mbid=? AND w.status!='dropped'""",
        (old_mbid,),
    ).fetchall()
    albums: set[int] = set()
    track_wants: dict[str, set[int]] = defaultdict(set)
    for want_id, scope, recording in wants:
        if scope != repo.Scope.TRACK:
            albums.add(want_id)
        elif recording not in recordings:
            return None
        else:
            track_wants[recording].add(want_id)
    return bool(wants), albums, track_wants


def _insert_replacement(
    conn: sqlite3.Connection,
    new: Release,
    decision: dict[str, object],
    has_wants: bool,
    albums: set[int],
    track_wants: dict[str, set[int]],
) -> dict[str, list[tuple[int, bool]]]:
    new_tracks: dict[str, list[tuple[int, bool]]] = defaultdict(list)
    repo.upsert_release(conn, new, decision)
    for medium in new.media:
        tracks = [track for track in medium.tracks if track.position > 0 and track.number != "0"]
        for track in tracks:
            if has_wants and not albums and track.recording_mbid not in track_wants:
                continue
            track_id, created = repo.insert_track(conn, new, medium, len(tracks), track)
            new_tracks[track.recording_mbid].append((track_id, created))
            for want_id in albums | track_wants[track.recording_mbid]:
                repo.link_track(conn, want_id, track_id)
    return new_tracks


def _relocate_tracks(
    conn: sqlite3.Connection,
    old_tracks: list[tuple[int, str, str]],
    new_tracks: dict[str, list[tuple[int, bool]]],
) -> None:
    for track_id, recording, state in old_tracks:
        if state == TrackState.SEARCHING:
            transition(
                conn,
                track_id,
                TrackState.SEARCHING,
                TrackState.PLANNED,
                Actor.RUNNER,
                expected_pin=None,
            )
        if targets := new_tracks.get(recording):
            conn.execute(
                "UPDATE attempts SET track_id=? WHERE track_id=?", (targets[0][0], track_id)
            )
            conn.execute(
                """INSERT OR IGNORE INTO want_tracks (want_id,track_id)
                SELECT want_id,? FROM want_tracks WHERE track_id=?""",
                (targets[0][0], track_id),
            )
            if state == TrackState.SKIPPED:
                for dest, created in targets:
                    if created:
                        transition(conn, dest, TrackState.PLANNED, TrackState.SKIPPED, Actor.USER)
        conn.execute(
            "DELETE FROM tracks WHERE id=? AND state IN ('planned','missing','skipped')",
            (track_id,),
        )


def switch_edition(
    conn: sqlite3.Connection,
    old: Release,
    new: Release,
    reason: str,
    *,
    planning: PlanConfig | None = None,
    searching: Collection[int] = (),
) -> bool:
    if old.mbid == new.mbid or old.release_group.mbid != new.release_group.mbid:
        return False
    choice = choose_release([new], planning or PlanConfig())
    if choice is None:
        return False
    _, decision = choice
    decision.update(switched_from=old.mbid, switched_reason=reason)
    recordings = {track.recording_mbid for track in release_tracks(new)}
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if not can_switch(conn, old):
            return False
        old_tracks = conn.execute(
            "SELECT id, recording_mbid, state FROM tracks WHERE release_mbid=?", (old.mbid,)
        ).fetchall()
        if not old_tracks or any(
            row[2] == "searching" and row[0] not in searching for row in old_tracks
        ):
            return False
        links = _want_links(conn, old.mbid, recordings)
        if links is None:
            return False
        new_tracks = _insert_replacement(conn, new, decision, *links)
        _relocate_tracks(conn, old_tracks, new_tracks)
        conn.execute("UPDATE releases SET switched_to=NULL WHERE mbid=?", (new.mbid,))
        conn.execute("UPDATE releases SET switched_to=? WHERE mbid=?", (new.mbid, old.mbid))
    return True
