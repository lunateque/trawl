import asyncio
import shutil
from pathlib import Path

from trawl.core.audio import LOSSLESS, AudioTools
from trawl.core.catalog import Release, Track
from trawl.core.fetch import (
    Candidate,
    CandidateItem,
    Fetched,
    SourceWall,
)
from trawl.db import queue
from trawl.db.queue import Work
from trawl.library.repack import repack_needed
from trawl.runner.context import RunContext
from trawl.runner.fetch import fetch
from trawl.sources.base import Source
from trawl.sources.local import LocalSource, stage_file


async def pinned(context: RunContext, row: Work, track: Track) -> None:
    if row.pin is None:
        raise ValueError("Cannot fetch a pinned track without a pin")
    pin = queue.Pin.load(row.pin)
    name, ref = pin.source, pin.ref
    remaining = {track.mbid: row}
    candidate = Candidate(
        name,
        ref,
        "track",
        track.title,
        None,
        None,
        False,
        False,
        (
            CandidateItem(
                pin.item_ref or ref,
                None,
                track.length_ms,
                track.title,
                size=pin.size,
                expected_length_ms=track.length_ms,
            ),
        ),
    )
    if name == "path":
        source: Source = _LocalPin(context.deps.tools, context.cfg.sources.lossless_repack)
    elif name == "local":
        source = LocalSource(
            (),
            move=context.cfg.sources.local.move,
            lossless_repack=context.cfg.sources.lossless_repack,
        )
    else:
        selected = next((source for source in context.deps.sources if source.name == name), None)
        if selected is None:
            with context.conn:
                queue.attempt(
                    context.conn, row.id, name, ref, None, "fetch_failed", "source not configured"
                )
            context.missing(row, pinned=True)
            return
        source = selected
    try:
        if until := queue.pause_until(context.conn, source.name):
            context.requeue(remaining, until)
            return
        await fetch(
            context,
            source,
            candidate,
            {track.mbid: candidate.items[0]},
            remaining,
            {track.mbid: track},
            False,
        )
    except SourceWall as exc:
        with context.conn:
            until = queue.wall(context.conn, source.name, exc.reason)
            if remaining:
                queue.attempt(
                    context.conn,
                    row.id,
                    name,
                    pin.item_ref or ref,
                    None,
                    "fetch_failed",
                    exc.reason,
                    candidate_key=ref,
                )
        context.requeue(remaining, until)
        return
    if remaining:
        context.missing(row, pinned=True)


class _LocalPin:
    name = "path"

    def __init__(self, tools: AudioTools, lossless_repack: bool) -> None:
        self.tools = tools
        self.lossless_repack = lossless_repack

    async def search(self, release: Release, tracks: list[Track]) -> list[Candidate]:
        return []

    async def fetch(self, candidate: Candidate, into: Path) -> list[Fetched]:
        original = Path(candidate.ref)
        if repack_needed(original):
            return [
                await asyncio.to_thread(
                    stage_file,
                    candidate.ref,
                    original,
                    into,
                    0,
                    move=False,
                    lossless_repack=self.lossless_repack,
                )
            ]
        path = into / f"audio{original.suffix}"
        await asyncio.to_thread(shutil.copyfile, original, path)
        audio = await asyncio.to_thread(self.tools.probe, path)
        return [Fetched(candidate.ref, path, audio.codec, audio.bitrate, audio.codec in LOSSLESS)]
