import asyncio
import logging
from dataclasses import replace
from pathlib import Path, PurePath

from trawl.core.catalog import Release, Track
from trawl.core.paths import write_atomic
from trawl.db import queue
from trawl.db.queue import Work
from trawl.db.states import TrackState as State
from trawl.library.layout import render, resolve_collision
from trawl.library.place import place
from trawl.runner.context import METADATA_ERRORS, RunContext
from trawl.tag.album import AlbumTags
from trawl.tag.mapping import fields_for
from trawl.verify.probe import AudioError

log = logging.getLogger("trawl.runner")
_RETRY = 900


async def finish(
    context: RunContext, release: Release, tracks: dict[str, Track], ids: set[int]
) -> None:
    tags = AlbumTags(context.cfg, context.deps.http, context.deps.mb, context.deps.tools, release)
    for row in queue.pending(context.conn, release_mbid=release.mbid):
        if context.stopping():
            return
        if row.id not in ids or row.path is None or row.state not in {State.VERIFIED, State.TAGGED}:
            continue
        if (track := tracks.get(row.track_mbid)) is None:
            continue
        path = row.path
        try:
            if row.state == State.VERIFIED:
                fields = await tags.write(path, track)
                with context.conn:
                    if not context.change(row, State.TAGGED):
                        continue
                row = replace(row, state=State.TAGGED)
            else:
                fields = fields_for(release, track, context.cfg.genres, ())
            relative = render(context.cfg.template, fields, path.suffix, len(release.media))
            place_track(context, row, relative)
        except (*METADATA_ERRORS, AudioError) as exc:
            log.error("Cannot finish track %s: %s", row.id, exc)


def place_track(context: RunContext, row: Work, relative: PurePath) -> None:
    staged = row.path
    if staged is None:
        return
    library = context.stage.parent
    with context.conn:
        context.conn.execute("BEGIN IMMEDIATE")
        current = context.conn.execute("SELECT state FROM tracks WHERE id=?", (row.id,)).fetchone()
        if current is None or current[0] != State.TAGGED:
            return
        owners: dict[str, set[str]] = {}
        for raw, mbid in context.conn.execute(
            "SELECT path,release_mbid FROM tracks WHERE state='done' AND path IS NOT NULL"
        ):
            path = Path(raw)
            key = (path if relative.parent == PurePath() else path.parent).as_posix().casefold()
            owners.setdefault(key, set()).add(mbid)

        def owner(path: PurePath) -> str | None:
            key = library / (path if path.parent == PurePath() else path.parent)
            return next(
                (
                    mbid
                    for mbid in owners.get(key.as_posix().casefold(), ())
                    if mbid != row.release_mbid
                ),
                None,
            )

        final = library / resolve_collision(relative, row.release_mbid, owner)
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            cover = staged.parent / "cover.jpg"
            cover_target = final.parent / "cover.jpg"
            if cover.is_file() and not cover_target.exists():
                write_atomic(cover_target, cover.read_bytes())
            lrc = staged.with_suffix(".lrc")
            if lrc.is_file():
                write_atomic(final.with_suffix(".lrc"), lrc.read_bytes())
            success = place(staged, final)
        except PermissionError:
            success = False
        if success:
            context.change(row, State.DONE, path=str(final), next_try_at=None)
        else:
            context.change(row, State.TAGGED, next_try_at=queue.after(_RETRY))


async def gain(context: RunContext) -> None:
    for mbid in queue.gain_releases(context.conn):
        if context.stopping():
            return
        paths = queue.done_paths(context.conn, mbid)
        try:
            release = await context.get_release(mbid)
            if len(paths) != release.track_count:
                continue
            await asyncio.to_thread(context.deps.tools.album_gain, paths)
        except (*METADATA_ERRORS, AudioError) as exc:
            log.error("Cannot write album gain for %s: %s", mbid, exc)
            with context.conn:
                queue.set_gain(context.conn, mbid, queue.after(_RETRY))
        else:
            with context.conn:
                queue.set_gain(context.conn, mbid, None)
