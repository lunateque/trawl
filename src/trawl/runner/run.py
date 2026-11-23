import asyncio
import logging
import sqlite3
from dataclasses import dataclass, replace
from itertools import groupby

from trawl.config.models import Config, Secrets
from trawl.core.catalog import Release, Track, release_tracks
from trawl.db import queue, repo
from trawl.db.queue import Work
from trawl.db.states import TrackState as State
from trawl.library.place import cleanup_staging
from trawl.runner.context import METADATA_ERRORS, RunContext, RunDeps
from trawl.runner.finalize import finish, gain
from trawl.runner.pin import pinned
from trawl.runner.progress import Progress
from trawl.runner.search import search

log = logging.getLogger("trawl.runner")


@dataclass(frozen=True)
class RunReport:
    states: list[tuple[str, int]]
    outcomes: list[tuple[str, int, int]]
    pauses: list[queue.Pause]


async def run_once(
    conn: sqlite3.Connection,
    cfg: Config,
    secrets: Secrets,
    deps: RunDeps,
    *,
    only: int | None = None,
    release_mbid: str | None = None,
    stop: asyncio.Event | None = None,
    progress: Progress | None = None,
) -> RunReport:
    if not secrets.acoustid_key:
        raise ValueError(
            "Configure acoustid_key in secrets.toml (https://acoustid.org/new-application)"
        )
    runner = RunContext(conn, cfg, deps, stop, progress)
    cleanup_staging(conn, runner.stage)
    start = conn.execute("SELECT COALESCE(max(id),0) FROM attempts").fetchone()[0]
    groups = [
        list(rows)
        for _, rows in groupby(
            queue.pending(conn, only, release_mbid=release_mbid), key=lambda row: row.release_mbid
        )
    ]
    limit = asyncio.Semaphore(
        max(cfg.sources.ytmusic_concurrency, cfg.sources.soulseek.concurrency)
    )

    async def run_group(rows: list[Work]) -> None:
        async with limit:
            if stop is None or not stop.is_set():
                await _run_release(runner, rows)

    async with asyncio.TaskGroup() as tasks:
        for rows in groups:
            tasks.create_task(run_group(rows))
    await gain(runner)
    cleanup_staging(conn, runner.stage)
    return RunReport(
        repo.status(conn).tracks, queue.attempt_counts(conn, start), queue.pauses(conn)
    )


async def _prepare_release(
    context: RunContext, release: Release, rows: list[Work]
) -> tuple[dict[str, Track], dict[str, Work]] | None:
    canonical = {track.mbid: track for track in release_tracks(release)}
    misses: dict[str, Work] = {}
    tracks: dict[str, Track] = {}
    for row in rows:
        if context.stopping():
            return None
        if (track := canonical.get(row.track_mbid)) is None:
            log.error("Track %s has disappeared from release %s", row.track_mbid, release.mbid)
            continue
        tracks[track.mbid] = replace(
            track, title=row.title, recording_mbid=row.recording_mbid, length_ms=row.length_ms
        )
        if row.state == State.PLANNED and row.pin is None:
            reused = await context.reuse(row)
            if reused is not None:
                continue
        if row.state in (State.FETCHED, State.PLANNED):
            with context.conn:
                if not context.change(row, State.SEARCHING, path=None):
                    continue
            row = replace(row, state=State.SEARCHING, path=None)
        if row.state == State.SEARCHING:
            misses[row.track_mbid] = row
    return tracks, misses


async def _run_initial_pins(
    context: RunContext, tracks: dict[str, Track], misses: dict[str, Work]
) -> bool:
    for row in list(misses.values()):
        if context.stopping():
            return False
        if row.pin is not None:
            await pinned(context, row, tracks[row.track_mbid])
            del misses[row.track_mbid]
    return True


async def _run_new_pins(
    context: RunContext, mbid: str, initial_pins: dict[int, str | None], tracks: dict[str, Track]
) -> bool:
    # A pin written during an await takes precedence over the automatic result.
    for row in queue.pending(context.conn, release_mbid=mbid):
        if context.stopping():
            return False
        if (
            row.id in initial_pins
            and row.state == State.SEARCHING
            and row.pin is not None
            and row.pin != initial_pins[row.id]
            and (track := tracks.get(row.track_mbid)) is not None
        ):
            await pinned(context, row, track)
    return True


async def _run_release(context: RunContext, rows: list[Work], *, allow_switch: bool = True) -> None:
    mbid = rows[0].release_mbid
    try:
        release = await context.get_release(mbid)
    except METADATA_ERRORS as exc:
        log.error("Cannot load release %s: %s", mbid, exc)
        return
    prepared = await _prepare_release(context, release, rows)
    if prepared is None:
        return
    tracks, misses = prepared
    if not await _run_initial_pins(context, tracks, misses):
        return
    if misses:
        selected = await search(context, release, tracks, misses, allow_switch=allow_switch)
        if selected:
            replacement = queue.pending(context.conn, release_mbid=selected.mbid)
            if replacement:
                await _run_release(context, replacement, allow_switch=False)
            return
    ids = {row.id for row in rows}
    initial_pins = {row.id: row.pin for row in rows}
    if not await _run_new_pins(context, mbid, initial_pins, tracks):
        return
    await finish(context, release, tracks, ids)
