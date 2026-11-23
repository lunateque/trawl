import asyncio
import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from trawl.config.models import Config
from trawl.core.audio import AudioTools
from trawl.core.catalog import MbDataError, Release
from trawl.db import queue
from trawl.db.queue import Work
from trawl.db.states import Actor, transition
from trawl.db.states import TrackState as State
from trawl.library.place import staging_dir
from trawl.mb.client import MusicBrainzClient
from trawl.net.client import HttpClient, HttpError
from trawl.runner.progress import Progress
from trawl.runner.tools import default_audio_tools
from trawl.sources.base import Source
from trawl.verify.fingerprint import AcoustId

log = logging.getLogger("trawl.runner")
METADATA_ERRORS = (HttpError, MbDataError, OSError, ValueError)


@dataclass(frozen=True)
class RunDeps:
    http: HttpClient
    mb: MusicBrainzClient
    sources: list[Source]
    acoustid: AcoustId
    tools: AudioTools = field(default_factory=default_audio_tools)


class RunContext:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        deps: RunDeps,
        stop: asyncio.Event | None,
        progress: Progress | None,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.deps = deps
        self.stop = stop
        self.progress = progress
        self.stage = staging_dir(cfg.library_root)
        self.releases: dict[str, Release] = {}
        self.youtube = asyncio.Semaphore(cfg.sources.ytmusic_concurrency)

    def stopping(self) -> bool:
        return self.stop is not None and self.stop.is_set()

    def change(self, row: Work, state: State, **fields: object) -> bool:
        return transition(
            self.conn,
            row.id,
            row.state,
            state,
            Actor.RUNNER,
            expected_pin=row.pin,
            **fields,
        )

    def workspace(self) -> Path:
        return Path(tempfile.mkdtemp(dir=self.stage))

    async def get_release(self, mbid: str) -> Release:
        if mbid not in self.releases:
            self.releases[mbid] = await self.deps.mb.release(mbid)
        return self.releases[mbid]

    async def reuse(self, row: Work) -> Work | None:
        if (old := queue.reusable(self.conn, row.recording_mbid)) is None:
            return None
        try:
            dest = self.workspace() / f"audio{old.path.suffix}"
            await asyncio.to_thread(shutil.copyfile, old.path, dest)
        except OSError as exc:
            log.warning("Cannot reuse track %s: %s", row.id, exc)
            return None
        with self.conn:
            if self.change(
                row,
                State.VERIFIED,
                path=str(dest),
                source=old.source,
                codec=old.codec,
                lossless=old.lossless,
                bitrate=old.bitrate,
                verified_by="reuse",
                next_try_at=None,
            ):
                return replace(row, state=State.VERIFIED, path=dest)
        return None

    def requeue(self, misses: dict[str, Work], until: str) -> None:
        with self.conn:
            for row in misses.values():
                self.change(row, State.PLANNED, next_try_at=until, path=None)

    def missing(self, row: Work, *, pinned: bool = False) -> None:
        with self.conn:
            self.change(
                row,
                State.MISSING,
                path=None,
                pinned_candidate=row.pin if pinned else None,
                next_try_at=None if pinned else queue.after(self.cfg.retry_missing_days * 86400),
            )
