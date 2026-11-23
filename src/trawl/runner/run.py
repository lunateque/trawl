import asyncio
import logging
import shutil
import sqlite3
import tempfile
from collections import defaultdict, deque
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from itertools import groupby
from pathlib import Path, PurePath

from trawl.config import Config, Secrets
from trawl.db import queue, repo, review
from trawl.db.queue import Work
from trawl.db.states import Actor, transition
from trawl.db.states import TrackState as State
from trawl.library.layout import render, resolve_collision
from trawl.library.place import cleanup_staging, place, staging_dir, write_atomic
from trawl.library.repack import repack_needed
from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import MbDataError, Release, Track
from trawl.net.client import HttpClient, HttpError
from trawl.plan.import_folder import imported_folder
from trawl.plan.switch import can_switch, switch_edition
from trawl.runner.check import check, check_folder
from trawl.runner.progress import Progress
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
from trawl.sources.local import LocalSource, stage_file
from trawl.sources.score import coverage, match_items, release_tracks, score
from trawl.sources.soulseek import SoulseekSource
from trawl.tag.album import AlbumTags
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
    release_mbid: str | None = None,
    stop: asyncio.Event | None = None,
    progress: Progress | None = None,
) -> RunReport:
    if not secrets.acoustid_key:
        raise ValueError(
            "Configure acoustid_key in secrets.toml (https://acoustid.org/new-application)"
        )
    runner = _Runner(conn, cfg, deps, stop, progress)
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

    async def _get_release(self, mbid: str) -> Release:
        if mbid not in self.releases:
            self.releases[mbid] = await self.deps.mb.release(mbid)
        return self.releases[mbid]

    async def release(self, rows: list[Work], *, allow_switch: bool = True) -> None:
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
            if self.stopping():
                return
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
            if self.stopping():
                return
            if row.pin is not None:
                await self.pinned(row, tracks[row.track_mbid])
                del misses[row.track_mbid]
        if misses:
            selected = await self.search(release, tracks, misses, allow_switch=allow_switch)
            if selected:
                replacement = queue.pending(self.conn, release_mbid=selected.mbid)
                if replacement:
                    await self.release(replacement, allow_switch=False)
                return
        ids = {row.id for row in rows}
        initial_pins = {row.id: row.pin for row in rows}
        # A pin written during an await takes precedence over the automatic result.
        for row in queue.pending(self.conn, release_mbid=mbid):
            if self.stopping():
                return
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
                pinned_candidate=row.pin if pinned else None,
                next_try_at=None if pinned else queue.after(self.cfg.retry_missing_days * 86400),
            )

    async def folder_candidates(
        self,
        source: LocalSource | SoulseekSource,
        release: Release,
        basis: list[Track],
        misses: dict[str, Work],
        album: bool,
        allow_switch: bool,
    ) -> tuple[Release | None, list[tuple[Candidate, dict[str, CandidateItem], bool]]]:
        editions = [(release, basis)]
        if allow_switch and can_switch(self.conn, release):
            try:
                others = await self.deps.mb.release_group_releases(release.release_group.mbid)
                recordings = {track.recording_mbid for track in basis}
                for other in others:
                    if other.mbid == release.mbid:
                        continue
                    full = await self._get_release(other.mbid)
                    tracks = release_tracks(full)
                    if not album:
                        tracks = [track for track in tracks if track.recording_mbid in recordings]
                        if {track.recording_mbid for track in tracks} != recordings:
                            continue
                    if tracks:
                        editions.append((full, tracks))
            except (*_METADATA_ERRORS, KeyError) as exc:
                log.warning("Cannot compare editions of %s: %s", release.mbid, exc)
        threshold = self.cfg.sources.album_coverage if album else self.cfg.sources.track_coverage
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
                self.conn,
                release,
                best.release,
                reason,
                planning=self.cfg.plan,
                searching={row.id for row in misses.values()},
            ):
                return best.release, []
        return None, [(match.candidate, match.mapping, match.reliable) for match in current]

    async def candidate_search(
        self,
        source: Source,
        release: Release,
        basis: list[Track],
        album: bool,
    ) -> list[tuple[Candidate, dict[str, CandidateItem], bool]]:
        threshold = self.cfg.sources.album_coverage if album else self.cfg.sources.track_coverage
        ranked: list[tuple[Candidate, dict[str, CandidateItem], bool]] = []
        for candidate in await source.search(release, basis):
            mapping = match_items(
                basis, candidate.items, release if candidate.kind == "album" else None
            )
            single_fallback = album and source.name == "ytmusic" and candidate.kind == "track"
            if not mapping or (not single_fallback and coverage(basis, mapping) < threshold):
                continue
            reliable = (
                candidate.kind == "album"
                and candidate.official
                and len(candidate.items) == release.track_count
                and len(mapping) == len(basis)
            )
            ranked.append(
                (replace(candidate, score=score(candidate, basis, mapping)), mapping, reliable)
            )
        return sorted(ranked, key=lambda entry: (entry[0].title_penalty, -entry[0].score))

    async def search(
        self,
        release: Release,
        tracks: dict[str, Track],
        misses: dict[str, Work],
        *,
        allow_switch: bool,
    ) -> Release | None:
        delayed: set[str] = set()
        tried: set[str] = set()
        with self.conn:
            for row in misses.values():
                review.clear_candidates(self.conn, row.id)
        imports = imported_folder(self.conn, release)
        for imported in imports:
            if self.stopping():
                return None
            source = LocalSource(
                (),
                move=self.cfg.sources.local.move,
                lossless_repack=self.cfg.sources.lossless_repack,
            )
            todo = {key: item for key, item in imported.mapping.items() if key in misses}
            if todo:
                tried.update(todo)
                self.remember([(imported.candidate, todo, imported.reliable)], misses)
                await self.fetch(
                    source, imported.candidate, todo, misses, tracks, imported.reliable
                )
            if not misses:
                return None
        for source in self.deps.sources:
            if self.stopping():
                return None
            if until := queue.pause_until(self.conn, source.name):
                delayed.add(until)
                continue
            targets = [tracks[mbid] for mbid in misses]
            album = any(row.album_scope for row in misses.values())
            basis = release_tracks(release) if album else targets
            try:
                if isinstance(source, LocalSource | SoulseekSource):
                    selected, ranked = await self.folder_candidates(
                        source, release, basis, misses, album, allow_switch and not imports
                    )
                    if selected:
                        return selected
                elif source.name == "ytmusic":
                    async with self.youtube:
                        ranked = await self.candidate_search(source, release, basis, album)
                else:
                    ranked = await self.candidate_search(source, release, basis, album)
                self.remember(ranked, misses)
                for candidate, mapping, reliable in ranked:
                    if self.stopping():
                        return None
                    if until := queue.pause_until(self.conn, source.name):
                        delayed.add(until)
                        break
                    todo = {mbid: item for mbid, item in mapping.items() if mbid in misses}
                    if not todo:
                        continue
                    tried.update(todo)
                    await self.fetch(source, candidate, todo, misses, tracks, reliable)
                    if not misses:
                        return None
            except SourceWall as exc:
                with self.conn:
                    until = queue.wall(self.conn, source.name, exc.reason)
                    for row in misses.values():
                        queue.attempt(
                            self.conn, row.id, source.name, None, None, "fetch_failed", exc.reason
                        )
                delayed.add(until)
            except (FetchError, MbDataError, OSError) as exc:
                with self.conn:
                    for mbid, row in misses.items():
                        tried.add(mbid)
                        queue.attempt(
                            self.conn, row.id, source.name, None, None, "fetch_failed", str(exc)
                        )
        if self.stopping():
            return None
        if delayed:
            self.requeue(misses, min(delayed))
            return None
        for mbid, row in misses.items():
            if mbid not in tried:
                with self.conn:
                    queue.attempt(
                        self.conn,
                        row.id,
                        self.deps.sources[-1].name if self.deps.sources else "local",
                        None,
                        None,
                        "no_candidates",
                        "no eligible source candidates",
                    )
            self.missing(row)
        return None

    def remember(
        self,
        ranked: list[tuple[Candidate, dict[str, CandidateItem], bool]],
        misses: dict[str, Work],
    ) -> None:
        with self.conn:
            for candidate, mapping, _ in ranked:
                for mbid, item in mapping.items():
                    if (row := misses.get(mbid)) is not None:
                        review.remember(self.conn, row.id, candidate, item)

    async def pinned(self, row: Work, track: Track) -> None:
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
            source: Source = _LocalPin(self.deps.tools, self.cfg.sources.lossless_repack)
        elif name == "local":
            source = LocalSource(
                (),
                move=self.cfg.sources.local.move,
                lossless_repack=self.cfg.sources.lossless_repack,
            )
        else:
            selected = next((source for source in self.deps.sources if source.name == name), None)
            if selected is None:
                with self.conn:
                    queue.attempt(
                        self.conn, row.id, name, ref, None, "fetch_failed", "source not configured"
                    )
                self.missing(row, pinned=True)
                return
            source = selected
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
                if remaining:
                    queue.attempt(
                        self.conn,
                        row.id,
                        name,
                        pin.item_ref or ref,
                        None,
                        "fetch_failed",
                        exc.reason,
                        candidate_key=ref,
                    )
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
        fetcher = (
            LocalSource(source.inbox, lossless_repack=source.lossless_repack)
            if isinstance(source, LocalSource) and source.move
            else source
        )
        issues: dict[str, FetchIssue] = {}
        wall: SourceWall | None = None
        try:
            if source.name == "ytmusic":
                async with self.youtube:
                    files = await self.download(fetcher, limited)
            else:
                files = await self.download(fetcher, limited)
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
                        candidate_key=candidate.ref,
                    )
            return
        with self.conn:
            if files and wall is None:
                queue.source_succeeded(self.conn, source.name)
        by_ref: dict[str, deque[Fetched]] = defaultdict(deque)
        for file in files:
            by_ref[file.item_ref].append(file)
        selected: dict[str, Fetched] = {}
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
                            candidate_key=candidate.ref,
                        )
                continue
            selected[mbid] = fetched.popleft()
        if candidate.kind == "album" and isinstance(source, LocalSource | SoulseekSource):
            checked_files = await check_folder(
                {key: tracks[key] for key in todo},
                selected,
                reliable,
                self.deps.acoustid,
                self.deps.mb,
                self.cfg.acoustid_min_score,
                self.deps.tools,
            )
        else:
            checked_files = {}
            for key, file in selected.items():
                checked_files[key] = (
                    file,
                    await check(
                        tracks[key],
                        file.path,
                        reliable,
                        self.deps.acoustid,
                        self.deps.mb,
                        self.cfg.acoustid_min_score,
                        self.deps.tools,
                    ),
                )
        mismatch = any(checked.mismatch for _, checked in checked_files.values())
        for mbid, (file, checked) in checked_files.items():
            row = misses[mbid]
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
            row = replace(row, state=State.FETCHED)
            decision = checked.with_folder_guard(mismatch)
            retain = decision.outcome == "rejected_verify"
            wait_for_user = retain and row.pin is not None
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
                    if changed and wait_for_user:
                        changed = self.change(
                            replace(row, state=State.SEARCHING),
                            State.MISSING,
                            next_try_at=None,
                        )
                if changed and retain:
                    review.reject(
                        self.conn,
                        row.id,
                        source.name,
                        candidate.ref,
                        candidate.score,
                        decision.reason,
                        file,
                        checked.comparison,
                    )
                elif changed:
                    queue.attempt(
                        self.conn,
                        row.id,
                        source.name,
                        file.item_ref,
                        candidate.score,
                        decision.outcome,
                        decision.reason,
                        candidate_key=candidate.ref,
                    )
            if decision.accept and changed and isinstance(source, LocalSource) and source.move:
                try:
                    await asyncio.to_thread(Path(file.item_ref).unlink)
                except OSError as exc:
                    log.warning("Cannot remove local original %s: %s", file.item_ref, exc)
            if decision.accept or wait_for_user or not changed:
                del misses[mbid]
        if wall is not None:
            raise wall

    async def download(self, source: Source, candidate: Candidate) -> list[Fetched]:
        activity = (
            self.progress.downloading(source.name, candidate.title)
            if self.progress is not None
            else nullcontext()
        )
        with activity:
            return await source.fetch(candidate, self.workspace())

    async def finish(self, release: Release, tracks: dict[str, Track], ids: set[int]) -> None:
        tags = AlbumTags(self.cfg, self.deps.http, self.deps.mb, self.deps.tools, release)
        for row in queue.pending(self.conn, release_mbid=release.mbid):
            if self.stopping():
                return
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
                    fields = await tags.write(path, track)
                    with self.conn:
                        if not self.change(row, State.TAGGED):
                            continue
                    row = replace(row, state=State.TAGGED)
                else:
                    fields = fields_for(release, track, self.cfg.genres, ())
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
                    write_atomic(cover_target, cover.read_bytes())
                lrc = staged.with_suffix(".lrc")
                if lrc.is_file():
                    write_atomic(final.with_suffix(".lrc"), lrc.read_bytes())
                success = place(staged, final)
            except PermissionError:
                success = False
            if success:
                self.change(row, State.DONE, path=str(final), next_try_at=None)
            else:
                self.change(row, State.TAGGED, next_try_at=queue.after(_RETRY))

    async def gain(self) -> None:
        for mbid in queue.gain_releases(self.conn):
            if self.stopping():
                return
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
