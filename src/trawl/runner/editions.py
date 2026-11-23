import logging

from trawl.core.catalog import Release, Track, release_tracks
from trawl.core.fetch import (
    Candidate,
    CandidateItem,
)
from trawl.db.queue import Work
from trawl.plan.switch import can_switch, switch_edition
from trawl.runner.context import METADATA_ERRORS, RunContext
from trawl.sources.local import LocalSource
from trawl.sources.soulseek import SoulseekSource

log = logging.getLogger("trawl.runner")


async def folder_candidates(
    context: RunContext,
    source: LocalSource | SoulseekSource,
    release: Release,
    basis: list[Track],
    misses: dict[str, Work],
    album: bool,
    allow_switch: bool,
) -> tuple[Release | None, list[tuple[Candidate, dict[str, CandidateItem], bool]]]:
    editions = [(release, basis)]
    if allow_switch and can_switch(context.conn, release):
        try:
            others = await context.deps.mb.release_group_releases(release.release_group.mbid)
            recordings = {track.recording_mbid for track in basis}
            for other in others:
                if other.mbid == release.mbid:
                    continue
                full = await context.get_release(other.mbid)
                tracks = release_tracks(full)
                if not album:
                    tracks = [track for track in tracks if track.recording_mbid in recordings]
                    if {track.recording_mbid for track in tracks} != recordings:
                        continue
                if tracks:
                    editions.append((full, tracks))
        except (*METADATA_ERRORS, KeyError) as exc:
            log.warning("Cannot compare editions of %s: %s", release.mbid, exc)
    threshold = context.cfg.sources.album_coverage if album else context.cfg.sources.track_coverage
    matches = await source.find(release, editions, album=album, min_coverage=threshold)
    current = [match for match in matches if match.release.mbid == release.mbid]
    best = matches[0] if matches else None
    if (
        best
        and best.release.mbid != release.mbid
        and (not current or best.candidate.score > current[0].candidate.score)
    ):
        reason = f"{source.name} folder fits this edition better"
        if switch_edition(
            context.conn,
            release,
            best.release,
            reason,
            planning=context.cfg.plan,
            searching={row.id for row in misses.values()},
        ):
            return best.release, []
    return None, [(match.candidate, match.mapping, match.reliable) for match in current]
