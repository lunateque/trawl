import asyncio
import json
import logging
import shutil
import sqlite3
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from itertools import groupby
from pathlib import Path, PurePath

from trawl.config import Config, Secrets
from trawl.db import queue, repo
from trawl.db.queue import Work
from trawl.db.states import Actor, transition
from trawl.db.states import TrackState as State
from trawl.library.layout import render, resolve_collision
from trawl.library.place import cleanup_staging, place, staging_dir
from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import MbDataError, Release, Track, object_value
from trawl.net.client import HttpClient, HttpError
from trawl.runner.check import Checked, check
from trawl.runner.tools import AudioTools
from trawl.sources.base import (
    Candidate,
    CandidateItem,
    Fetched,
    FetchError,
    FetchIssue,
    PartialFetchError,
    Source,
    SourceWall,
)
from trawl.sources.score import ALBUM_COVERAGE, coverage, match_items, release_tracks, score
from trawl.tag.cover import Cover, fetch_cover
from trawl.tag.lyrics import fetch_lyrics
from trawl.tag.mapping import fields_for
from trawl.verify.fingerprint import AcoustId
from trawl.verify.probe import LOSSLESS, AudioError

log = logging.getLogger("trawl.runner")
_RETRY = 900
_METADATA_ERRORS = (HttpError, MbDataError, OSError, ValueError)


@dataclass(frozen=True)
class RunDeps:
    http: HttpClient
    mb: MusicBrainzClient
    sources: list[Source]
    acoustid: AcoustId
    tools: AudioTools = field(default_factory=AudioTools)


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
) -> RunReport:
    if not secrets.acoustid_key:
        raise ValueError(
            "Configure acoustid_key in secrets.toml (https://acoustid.org/new-application)"
        )
    runner = _Runner(conn, cfg, deps)
    cleanup_staging(conn, runner.stage)
    start = conn.execute("SELECT COALESCE(max(id),0) FROM attempts").fetchone()[0]
    groups = [
        list(rows)
        for _, rows in groupby(queue.pending(conn, only), key=lambda row: row.release_mbid)
    ]
    limit = asyncio.Semaphore(cfg.sources.ytmusic_concurrency)

    async def run_group(rows: list[Work]) -> None:
        async with limit:
            await runner.release(rows)

    async with asyncio.TaskGroup() as tasks:
        for rows in groups:
            tasks.create_task(run_group(rows))
    await runner.gain()
    cleanup_staging(conn, runner.stage)
    return RunReport(
        repo.status(conn).tracks, queue.attempt_counts(conn, start), queue.pauses(conn)
    )


class _Runner:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, deps: RunDeps) -> None:
        self.conn = conn
        self.cfg = cfg
        self.deps = deps
        self.stage = staging_dir(cfg.library_root)
        self.releases: dict[str, Release] = {}

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

    async def _get_release(self, mbid: str) -> Release:
        if mbid not in self.releases:
            self.releases[mbid] = await self.deps.mb.release(mbid)
        return self.releases[mbid]

    async def release(self, rows: list[Work]) -> None:
        mbid = rows[0].release_mbid
        try:
            release = await self._get_release(mbid)
        except _METADATA_ERRORS as exc:
            log.error("Cannot load release %s: %s", mbid, exc)
            return
        canonical = {track.mbid: track for track in release_tracks(release)}
        misses: dict[str, Work] = {}
        tracks: dict[str, Track] = {}
        for row in rows:
            if (track := canonical.get(row.track_mbid)) is None:
                log.error("Track %s has disappeared from release %s", row.track_mbid, mbid)
                continue
            tracks[track.mbid] = replace(
                track, title=row.title, recording_mbid=row.recording_mbid, length_ms=row.length_ms
            )
            if row.state == State.PLANNED and row.pin is None:
                reused = await self.reuse(row)
                if reused is not None:
                    continue
            if row.state in (State.FETCHED, State.PLANNED):
                with self.conn:
                    if not self.change(row, State.SEARCHING, path=None):
                        continue
                row = replace(row, state=State.SEARCHING, path=None)
            if row.state == State.SEARCHING:
                misses[row.track_mbid] = row
        for row in list(misses.values()):
            if row.pin is not None:
                await self.pinned(row, tracks[row.track_mbid])
                del misses[row.track_mbid]
        if misses:
            await self.search(release, tracks, misses)
        ids = {row.id for row in rows}
        initial_pins = {row.id: row.pin for row in rows}
        # A pin written during an await takes precedence over the automatic result.
        for row in queue.pending(self.conn, release_mbid=mbid):
            if (
                row.id in ids
                and row.state == State.SEARCHING
                and row.pin is not None
                and row.pin != initial_pins[row.id]
                and (track := tracks.get(row.track_mbid)) is not None
            ):
                await self.pinned(row, track)
        await self.finish(release, tracks, ids)

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
                pinned_candidate=None,
                next_try_at=None if pinned else queue.after(self.cfg.retry_missing_days * 86400),
            )

    async def search(
        self, release: Release, tracks: dict[str, Track], misses: dict[str, Work]
    ) -> None:
        tried: set[str] = set()
        for source in self.deps.sources:
            if until := queue.pause_until(self.conn, source.name):
                self.requeue(misses, until)
                return
            targets = [tracks[mbid] for mbid in misses]
            album = any(row.album_scope for row in misses.values())
            basis = release_tracks(release) if album else targets
            try:
                candidates = await source.search(release, basis)
                ranked: list[tuple[Candidate, dict[str, CandidateItem]]] = []
                for candidate in candidates:
                    mapping = match_items(
                        basis, candidate.items, release if candidate.kind == "album" else None
                    )
                    if not mapping or (album and coverage(basis, mapping) < ALBUM_COVERAGE):
                        continue
                    ranked.append(
                        (replace(candidate, score=score(candidate, basis, mapping)), mapping)
                    )
                for candidate, mapping in sorted(ranked, key=lambda entry: -entry[0].score):
                    if until := queue.pause_until(self.conn, source.name):
                        self.requeue(misses, until)
                        return
                    todo = {mbid: item for mbid, item in mapping.items() if mbid in misses}
                    if not todo:
                        continue
                    tried.update(todo)
                    reliable = (
                        candidate.kind == "album"
                        and len(mapping) == len(basis)
                        and (
                            album
                            or (candidate.official and len(candidate.items) == release.track_count)
                        )
                    )
                    await self.fetch(source, candidate, todo, misses, tracks, reliable)
                    if not misses:
                        return
            except SourceWall as exc:
                with self.conn:
                    until = queue.wall(self.conn, source.name, exc.reason)
                    for row in misses.values():
                        queue.attempt(
                            self.conn, row.id, source.name, None, None, "fetch_failed", exc.reason
                        )
                self.requeue(misses, until)
                return
            except (FetchError, MbDataError, OSError) as exc:
                with self.conn:
                    for mbid, row in misses.items():
                        tried.add(mbid)
                        queue.attempt(
                            self.conn, row.id, source.name, None, None, "fetch_failed", str(exc)
                        )
        for mbid, row in misses.items():
            if mbid not in tried:
                with self.conn:
                    queue.attempt(
                        self.conn,
                        row.id,
                        "ytmusic",
                        None,
                        None,
                        "no_candidates",
                        "no eligible source candidates",
                    )
            self.missing(row)

    async def pinned(self, row: Work, track: Track) -> None:
        pin = object_value(json.loads(row.pin or "{}"))
        name, ref = str(pin["source"]), str(pin["ref"])
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
            (CandidateItem(ref, None, track.length_ms, track.title),),
        )
        if name == "path":
            source: Source = _LocalPin(self.deps.tools)
        else:
            source = next(source for source in self.deps.sources if source.name == name)
        try:
            if until := queue.pause_until(self.conn, source.name):
                self.requeue(remaining, until)
                return
            await self.fetch(
                source,
                candidate,
                {track.mbid: candidate.items[0]},
                remaining,
                {track.mbid: track},
                False,
            )
        except SourceWall as exc:
            with self.conn:
                until = queue.wall(self.conn, source.name, exc.reason)
                queue.attempt(self.conn, row.id, name, ref, None, "fetch_failed", exc.reason)
            self.requeue(remaining, until)
            return
        if remaining:
            self.missing(row, pinned=True)

    async def fetch(
        self,
        source: Source,
        candidate: Candidate,
        todo: dict[str, CandidateItem],
        misses: dict[str, Work],
        tracks: dict[str, Track],
        reliable: bool,
    ) -> None:
        limited = replace(candidate, items=tuple(todo.values()))
        issues: dict[str, FetchIssue] = {}
        wall: SourceWall | None = None
        try:
            files = await source.fetch(limited, self.workspace())
        except PartialFetchError as exc:
            files, issues, wall = exc.files, exc.issues, exc.wall
        except (FetchError, OSError, AudioError) as exc:
            outcome: queue.Outcome = (
                "rejected_probe" if isinstance(exc, AudioError) else "fetch_failed"
            )
            with self.conn:
                for mbid, item in todo.items():
                    queue.attempt(
                        self.conn,
                        misses[mbid].id,
                        source.name,
                        item.ref,
                        candidate.score,
                        outcome,
                        str(exc),
                    )
            return
        with self.conn:
            if files and wall is None:
                queue.source_succeeded(self.conn, source.name)
        by_ref: dict[str, deque[Fetched]] = defaultdict(deque)
        for file in files:
            by_ref[file.item_ref].append(file)
        checks: dict[str, Checked] = {}
        for mbid, item in todo.items():
            row = misses[mbid]
            if not (fetched := by_ref.get(item.ref)):
                issue = issues.get(item.ref)
                if issue is not None or wall is None:
                    outcome = "rejected_probe" if issue and issue.probe_failed else "fetch_failed"
                    with self.conn:
                        queue.attempt(
                            self.conn,
                            row.id,
                            source.name,
                            item.ref,
                            candidate.score,
                            outcome,
                            issue.reason if issue else "file not downloaded",
                        )
                continue
            file = fetched.popleft()
            with self.conn:
                if not self.change(
                    row,
                    State.FETCHED,
                    path=str(file.path),
                    source=source.name,
                    codec=file.codec,
                    bitrate=file.bitrate,
                    lossless=file.lossless,
                ):
                    continue
            checks[mbid] = await check(
                tracks[mbid],
                file.path,
                reliable,
                self.deps.acoustid,
                self.deps.mb,
                self.cfg.acoustid_min_score,
                self.deps.tools,
            )
        mismatch = any(result.mismatch for result in checks.values())
        for mbid, checked in checks.items():
            row = replace(misses[mbid], state=State.FETCHED)
            decision = checked.with_folder_guard(mismatch)
            with self.conn:
                if decision.accept:
                    changed = self.change(
                        row,
                        State.VERIFIED,
                        verified_by=decision.verified_by,
                        pinned_candidate=None,
                        next_try_at=None,
                    )
                else:
                    changed = self.change(row, State.SEARCHING, path=None)
                if changed:
                    queue.attempt(
                        self.conn,
                        row.id,
                        source.name,
                        todo[mbid].ref,
                        candidate.score,
                        decision.outcome,
                        decision.reason,
                    )
            if decision.accept or not changed:
                del misses[mbid]
        if wall is not None:
            raise wall

    async def finish(self, release: Release, tracks: dict[str, Track], ids: set[int]) -> None:
        cover: Cover | None = None
        cover_loaded = False
        artist_genres: tuple[str, ...] = ()
        genres_loaded = False
        for row in queue.pending(self.conn, release_mbid=release.mbid):
            if (
                row.id not in ids
                or row.path is None
                or row.state not in {State.VERIFIED, State.TAGGED}
            ):
                continue
            if (track := tracks.get(row.track_mbid)) is None:
                continue
            path = row.path
            try:
                if row.state == State.VERIFIED:
                    if not cover_loaded:
                        cover = await fetch_cover(
                            self.deps.http, release.mbid, release.release_group.mbid
                        )
                        cover_loaded = True
                    if not genres_loaded and not release.genres and release.credits:
                        artist_genres = await self.deps.mb.artist_genres(
                            release.credits[0].artist_mbid
                        )
                        genres_loaded = True
                fields = fields_for(release, track, self.cfg.genres, artist_genres)
                if row.state == State.VERIFIED:
                    lyrics = None
                    if self.cfg.lyrics_enabled:
                        ms = track.length_ms
                        if ms is None:
                            ms = (await asyncio.to_thread(self.deps.tools.probe, path)).duration_ms
                        lyrics = await fetch_lyrics(
                            self.deps.http,
                            fields.artist,
                            fields.title,
                            fields.album,
                            round(ms / 1000),
                        )
                    await asyncio.to_thread(
                        self.deps.tools.write,
                        path,
                        fields,
                        cover.embed if cover else None,
                        lyrics.embedded if lyrics else None,
                    )
                    if lyrics and lyrics.synced:
                        await asyncio.to_thread(
                            path.with_suffix(".lrc").write_text, lyrics.synced, encoding="utf-8"
                        )
                    if cover:
                        await asyncio.to_thread((path.parent / "cover.jpg").write_bytes, cover.full)
                    await asyncio.to_thread(self.deps.tools.track_gain, path)
                    with self.conn:
                        if not self.change(row, State.TAGGED):
                            continue
                    row = replace(row, state=State.TAGGED)
                relative = render(self.cfg.template, fields, path.suffix, len(release.media))
                self.place(row, relative)
            except (*_METADATA_ERRORS, AudioError) as exc:
                log.error("Cannot finish track %s: %s", row.id, exc)

    def place(self, row: Work, relative: PurePath) -> None:
        staged = row.path
        if staged is None:
            return
        library = self.stage.parent
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            current = self.conn.execute("SELECT state FROM tracks WHERE id=?", (row.id,)).fetchone()
            if current is None or current[0] != State.TAGGED:
                return
            owners: dict[str, set[str]] = {}
            for raw, mbid in self.conn.execute(
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
                    shutil.copyfile(cover, cover_target)
                lrc = staged.with_suffix(".lrc")
                if lrc.is_file():
                    shutil.copyfile(lrc, final.with_suffix(".lrc"))
                success = place(staged, final)
            except PermissionError:
                success = False
            if success:
                self.change(row, State.DONE, path=str(final), next_try_at=None)
            else:
                self.change(row, State.TAGGED, next_try_at=queue.after(_RETRY))

    async def gain(self) -> None:
        for mbid in queue.gain_releases(self.conn):
            paths = queue.done_paths(self.conn, mbid)
            try:
                release = await self._get_release(mbid)
                if len(paths) != release.track_count:
                    continue
                await asyncio.to_thread(self.deps.tools.album_gain, paths)
            except (*_METADATA_ERRORS, AudioError) as exc:
                log.error("Cannot write album gain for %s: %s", mbid, exc)
                with self.conn:
                    queue.set_gain(self.conn, mbid, queue.after(_RETRY))
            else:
                with self.conn:
                    queue.set_gain(self.conn, mbid, None)


class _LocalPin:
    name = "path"

    def __init__(self, tools: AudioTools) -> None:
        self.tools = tools

    async def search(self, release: Release, tracks: list[Track]) -> list[Candidate]:
        return []

    async def fetch(self, candidate: Candidate, into: Path) -> list[Fetched]:
        original = Path(candidate.ref)
        path = into / f"audio{original.suffix}"
        await asyncio.to_thread(shutil.copyfile, original, path)
        audio = await asyncio.to_thread(self.tools.probe, path)
        return [Fetched(candidate.ref, path, audio.codec, audio.bitrate, audio.codec in LOSSLESS)]
