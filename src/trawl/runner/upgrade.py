import asyncio
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from trawl.config import Config
from trawl.db import queue
from trawl.db.states import Actor, TrackState, transition
from trawl.library.place import place, staging_dir
from trawl.mb.models import MbDataError, Release
from trawl.net.client import HttpError
from trawl.runner.check import check_folder
from trawl.runner.run import RunDeps
from trawl.sources.base import FetchError, PartialFetchError, SourceWall
from trawl.sources.local import LocalSource
from trawl.sources.score import release_tracks
from trawl.sources.soulseek import SoulseekSource
from trawl.tag.album import AlbumTags
from trawl.verify.probe import AudioError

log = logging.getLogger(__name__)
_RETRY = 900


@dataclass(frozen=True)
class UpgradeReport:
    checked: int
    replaced: int
    deferred: int


@dataclass(frozen=True)
class _LibraryTrack:
    id: int
    path: Path


def _replace_audio(
    conn: sqlite3.Connection, track: _LibraryTrack, staged: Path, final: Path, **fields: object
) -> None:
    old = track.path
    if final != old and (final.exists() or final.is_symlink()):
        raise OSError(f"Upgrade destination already exists: {final}")
    backup = staged.with_suffix(".original")
    installed = False
    saved = False
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT path FROM tracks WHERE id=? AND state='done' AND lossless=0", (track.id,)
            ).fetchone()
            if row is None or Path(row[0]) != old:
                raise OSError(f"Track {track.id} changed before the upgrade")
            if final == old:
                shutil.copyfile(old, backup)
                saved = True
            with staged.open("rb+") as stream:
                os.fsync(stream.fileno())
            if not place(staged, final):
                raise PermissionError(f"Cannot replace {final}")
            installed = True
            if final != old:
                old.replace(backup)
                saved = True
            if not transition(
                conn,
                track.id,
                TrackState.DONE,
                TrackState.DONE,
                Actor.RUNNER,
                expected_pin=False,
                path=str(final),
                **fields,
            ):
                raise OSError(f"Track {track.id} changed before the upgrade")
    except (OSError, sqlite3.Error):
        if installed:
            if saved:
                backup.replace(old)
            if final != old:
                final.unlink()
        raise


async def _gain(conn: sqlite3.Connection, deps: RunDeps, mbid: str) -> None:
    try:
        await asyncio.to_thread(deps.tools.album_gain, queue.done_paths(conn, mbid))
    except (AudioError, OSError) as exc:
        log.warning("Cannot refresh album gain for %s: %s", mbid, exc)
        with conn:
            queue.set_gain(conn, mbid, queue.after(_RETRY))
    else:
        with conn:
            queue.set_gain(conn, mbid, None)


async def _upgrade_release(
    conn: sqlite3.Connection,
    cfg: Config,
    deps: RunDeps,
    release: Release,
    stop: asyncio.Event | None,
) -> tuple[int, str | None]:
    targets = {
        mbid: _LibraryTrack(track_id, Path(path))
        for track_id, mbid, path in conn.execute(
            """SELECT id,release_track_mbid,path FROM tracks
            WHERE release_mbid=? AND state='done' AND lossless=0 AND path IS NOT NULL""",
            (release.mbid,),
        )
    }
    if not targets:
        return 0, None
    basis = release_tracks(release)
    tracks = {track.mbid: track for track in basis}
    staging = staging_dir(cfg.library_root)
    tags = AlbumTags(cfg, deps.http, deps.mb, deps.tools, release)
    replaced = 0
    delayed: set[str] = set()
    for source in deps.sources:
        if stop is not None and stop.is_set():
            break
        if not isinstance(source, LocalSource | SoulseekSource):
            continue
        if until := queue.pause_until(conn, source.name):
            delayed.add(until)
            continue
        fetcher = (
            LocalSource(source.inbox, lossless_repack=source.lossless_repack)
            if isinstance(source, LocalSource)
            else source
        )
        try:
            candidates = await source.find(
                release, [(release, basis)], album=True, lossless_only=True, min_coverage=0
            )
            for found in candidates:
                if stop is not None and stop.is_set():
                    break
                if not targets.keys() & found.mapping.keys():
                    continue
                with TemporaryDirectory(dir=staging) as raw_workspace:
                    workspace = Path(raw_workspace)
                    wall = None
                    try:
                        files = await fetcher.fetch(found.candidate, workspace)
                    except PartialFetchError as exc:
                        files, wall = exc.files, exc.wall
                    refs = {file.item_ref: file for file in files if file.lossless}
                    mapped = {
                        key: refs[item.ref]
                        for key, item in found.mapping.items()
                        if item.ref in refs
                    }
                    checks = await check_folder(
                        tracks,
                        mapped,
                        found.reliable,
                        deps.acoustid,
                        deps.mb,
                        cfg.acoustid_min_score,
                        deps.tools,
                    )
                    mismatch = any(checked.mismatch for _, checked in checks.values())
                    for key, (file, result) in checks.items():
                        if stop is not None and stop.is_set():
                            break
                        if (track := targets.get(key)) is None:
                            continue
                        decision = result.with_folder_guard(mismatch)
                        if decision.accept:
                            try:
                                await tags.write(file.path, tracks[key])
                                final = track.path.with_suffix(file.path.suffix)
                                _replace_audio(
                                    conn,
                                    track,
                                    file.path,
                                    final,
                                    source=source.name,
                                    codec=file.codec,
                                    bitrate=file.bitrate,
                                    lossless=True,
                                    verified_by=decision.verified_by,
                                )
                            except (AudioError, HttpError, MbDataError, OSError) as exc:
                                if isinstance(exc, PermissionError):
                                    delayed.add(queue.after(_RETRY))
                                with conn:
                                    queue.attempt(
                                        conn,
                                        track.id,
                                        source.name,
                                        file.item_ref,
                                        found.candidate.score,
                                        "fetch_failed",
                                        str(exc),
                                    )
                                continue
                            replaced += 1
                            del targets[key]
                            lrc = file.path.with_suffix(".lrc")
                            try:
                                if lrc.is_file():
                                    place(lrc, final.with_suffix(".lrc"))
                                if isinstance(source, LocalSource) and source.move:
                                    await asyncio.to_thread(Path(file.item_ref).unlink)
                            except OSError as exc:
                                log.warning(
                                    "Cannot finish upgrade side files for %s: %s", final, exc
                                )
                        with conn:
                            queue.attempt(
                                conn,
                                track.id,
                                source.name,
                                file.item_ref,
                                found.candidate.score,
                                decision.outcome,
                                decision.reason,
                            )
                    if wall is not None:
                        raise wall
                if not targets:
                    break
        except SourceWall as exc:
            with conn:
                delayed.add(queue.wall(conn, source.name, exc.reason))
        except (FetchError, OSError, MbDataError) as exc:
            log.warning("Cannot search for lossless files for %s: %s", release.mbid, exc)
        if not targets:
            break
    if replaced:
        await _gain(conn, deps, release.mbid)
    return replaced, min(delayed) if targets and delayed else None


async def upgrade_pass(
    conn: sqlite3.Connection,
    cfg: Config,
    deps: RunDeps,
    *,
    now: bool = False,
    stop: asyncio.Event | None = None,
) -> UpgradeReport:
    releases = [
        row[0]
        for row in conn.execute(
            """SELECT r.mbid FROM releases r
            WHERE (r.upgrade_next_try_at IS NULL OR r.upgrade_next_try_at <= ?1)
            AND (?2 OR r.upgrade_next_try_at IS NOT NULL
                    OR r.upgrade_checked_at IS NULL OR r.upgrade_checked_at <= ?3)
            AND EXISTS (SELECT 1 FROM tracks
                        WHERE release_mbid=r.mbid AND state='done' AND lossless=0)
            ORDER BY r.mbid""",
            (queue.now(), now, queue.after(-cfg.upgrade_older_than_days * 86400)),
        )
    ]
    if not any(isinstance(source, LocalSource | SoulseekSource) for source in deps.sources):
        return UpgradeReport(0, 0, 0)
    checked = replaced = deferred = 0
    for mbid in releases:
        if stop is not None and stop.is_set():
            break
        checked += 1
        try:
            release = await deps.mb.release(mbid)
            count, retry = await _upgrade_release(conn, cfg, deps, release, stop)
        except (HttpError, MbDataError, OSError) as exc:
            log.warning("Cannot check upgrades for %s: %s", mbid, exc)
            count, retry = 0, queue.after(_RETRY)
        replaced += count
        if stop is not None and stop.is_set():
            break
        with conn:
            conn.execute(
                "UPDATE releases SET upgrade_checked_at=?,upgrade_next_try_at=? WHERE mbid=?",
                (queue.now(), retry, mbid),
            )
        deferred += retry is not None
    return UpgradeReport(checked, replaced, deferred)
