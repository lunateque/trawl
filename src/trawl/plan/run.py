import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from trawl.config import PlanConfig
from trawl.db import repo
from trawl.db.repo import Kind, Scope, Want, WantStatus
from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import MbDataError, Release, ReleaseGroupRef
from trawl.net.client import HttpError
from trawl.plan.choose import choose_release, choose_release_group
from trawl.plan.wants import create_want

_LOG = logging.getLogger("trawl.plan")


@dataclass
class PlanReport:
    planned: int = 0
    empty: int = 0
    errors: int = 0
    tracks: int = 0


def sort_discography(
    groups: Sequence[ReleaseGroupRef],
    plays: Mapping[str, int] | None = None,
) -> list[ReleaseGroupRef]:
    counts: Mapping[str, int] = plays if plays is not None else {}
    return sorted(
        (group for group in groups if "Compilation" not in group.secondary_types),
        key=lambda group: (counts.get(group.mbid, 0), group.first_release_date or "", group.mbid),
        reverse=True,
    )


async def _select_release(
    mb: MusicBrainzClient,
    want: Want,
    cfg: PlanConfig,
) -> tuple[Release, dict[str, object], str | None] | None:
    recording_mbid = None
    if want.kind == Kind.RELEASE:
        edition = await mb.release(want.mbid)
        releases = [edition]
    elif want.kind == Kind.RELEASE_GROUP:
        releases = await mb.release_group_releases(want.mbid)
    else:
        recording = await mb.recording(want.mbid)
        related = await mb.recording_releases(recording.mbid)
        groups = {item.release_group.mbid: item.release_group for item in related}
        official = {item.release_group.mbid for item in related if item.status == "Official"}
        group = choose_release_group(list(groups.values()), official.__contains__)
        if group is None:
            return None
        editions = {item.mbid for item in related if item.release_group.mbid == group.mbid}
        releases = [
            release
            for release in await mb.release_group_releases(group.mbid)
            if release.mbid in editions
        ]
        if want.scope == Scope.TRACK:
            recording_mbid = recording.mbid
    choice = choose_release(releases, cfg)
    if choice is None:
        return None
    release, reason = choice
    if want.kind == Kind.RELEASE:
        reason["selected_by"] = "explicit release"
    else:
        # Browse omits metadata such as ISRCs; look up only the winning edition.
        release = await mb.release(release.mbid)
    return release, reason, recording_mbid


def store_tracks(
    conn: sqlite3.Connection,
    want: Want,
    release: Release,
    reason: dict[str, object],
    recording_mbid: str | None,
) -> tuple[bool, int]:
    selected = False
    inserted = 0
    for medium in release.media:
        tracks = [track for track in medium.tracks if track.position > 0 and track.number != "0"]
        for track in tracks:
            if recording_mbid is not None and track.recording_mbid != recording_mbid:
                continue
            if not selected:
                repo.upsert_release(conn, release, reason)
                selected = True
            track_id, created = repo.insert_track(conn, release, medium, len(tracks), track)
            repo.link_track(conn, want.id, track_id)
            inserted += created
    return selected, inserted


async def _plan_want(
    conn: sqlite3.Connection,
    mb: MusicBrainzClient,
    cfg: PlanConfig,
    want: Want,
) -> tuple[bool, int]:
    if want.kind == Kind.ARTIST:
        groups = sort_discography(await mb.artist_release_groups(want.mbid, cfg.types))
        children = groups[: want.limit if want.limit is not None else cfg.discography_limit]
        with conn:
            for group in children:
                create_want(
                    conn,
                    Kind.RELEASE_GROUP,
                    group.mbid,
                    Scope.ALBUM,
                    want.origin,
                    f"artist:{want.mbid}",
                )
            repo.set_want_status(
                conn, want.id, WantStatus.PLANNED if children else WantStatus.EMPTY
            )
        return bool(children), 0
    if want.kind == Kind.RECORDING:
        track_id = repo.track_for_recording(conn, want.mbid)
        if track_id is not None:
            with conn:
                repo.link_track(conn, want.id, track_id)
                repo.set_want_status(conn, want.id, WantStatus.PLANNED)
            return True, 0
    result = await _select_release(mb, want, cfg)
    if result and want.scope == Scope.ALBUM:
        release, reason, recording = result
        active = conn.execute(
            """WITH RECURSIVE editions(mbid) AS (
                SELECT ? UNION SELECT switched_to FROM releases JOIN editions USING (mbid)
                WHERE switched_to IS NOT NULL
            )
            SELECT mbid FROM releases JOIN editions USING (mbid) WHERE switched_to IS NULL""",
            (release.mbid,),
        ).fetchone()
        if active is not None and active[0] != release.mbid:
            result = await mb.release(active[0]), reason, recording
    with conn:
        planned, inserted = store_tracks(conn, want, *result) if result else (False, 0)
        repo.set_want_status(conn, want.id, WantStatus.PLANNED if planned else WantStatus.EMPTY)
    return planned, inserted


async def plan_new_wants(
    conn: sqlite3.Connection, mb: MusicBrainzClient, cfg: PlanConfig
) -> PlanReport:
    report = PlanReport()
    after = 0
    while (want := repo.next_want(conn, after)) is not None:
        after = want.id
        try:
            planned, inserted = await _plan_want(conn, mb, cfg, want)
        except (HttpError, MbDataError) as exc:
            _LOG.warning(
                "plan.musicbrainz_error", extra={"fields": {"want_id": want.id, "error": str(exc)}}
            )
            report.errors += 1
            continue
        report.tracks += inserted
        if planned:
            report.planned += 1
        else:
            report.empty += 1
    return report
