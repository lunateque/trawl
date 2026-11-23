import asyncio
import logging
from collections import defaultdict, deque
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

from trawl.core.catalog import Track
from trawl.core.fetch import (
    Candidate,
    CandidateItem,
    Fetched,
    FetchError,
    FetchIssue,
    PartialFetchError,
    SourceWall,
)
from trawl.db import queue, review
from trawl.db.queue import Work
from trawl.db.states import TrackState as State
from trawl.runner.check import Checked, check, check_folder
from trawl.runner.context import RunContext
from trawl.sources.base import Source
from trawl.sources.local import LocalSource
from trawl.sources.soulseek import SoulseekSource
from trawl.verify.probe import AudioError

log = logging.getLogger("trawl.runner")


async def _acquire(
    context: RunContext,
    source: Source,
    candidate: Candidate,
    todo: dict[str, CandidateItem],
    misses: dict[str, Work],
) -> tuple[list[Fetched], dict[str, FetchIssue], SourceWall | None] | None:
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
            async with context.youtube:
                files = await _download(context, fetcher, limited)
        else:
            files = await _download(context, fetcher, limited)
    except PartialFetchError as exc:
        files, issues, wall = exc.files, exc.issues, exc.wall
    except (FetchError, OSError, AudioError) as exc:
        outcome: queue.Outcome = "rejected_probe" if isinstance(exc, AudioError) else "fetch_failed"
        with context.conn:
            for mbid, item in todo.items():
                queue.attempt(
                    context.conn,
                    misses[mbid].id,
                    source.name,
                    item.ref,
                    candidate.score,
                    outcome,
                    str(exc),
                    candidate_key=candidate.ref,
                )
        return None
    with context.conn:
        if files and wall is None:
            queue.source_succeeded(context.conn, source.name)
    return files, issues, wall


def _select_files(
    context: RunContext,
    source: str,
    candidate: Candidate,
    todo: dict[str, CandidateItem],
    misses: dict[str, Work],
    files: list[Fetched],
    issues: dict[str, FetchIssue],
    wall: SourceWall | None,
) -> dict[str, Fetched]:
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
                with context.conn:
                    queue.attempt(
                        context.conn,
                        row.id,
                        source,
                        item.ref,
                        candidate.score,
                        outcome,
                        issue.reason if issue else "file not downloaded",
                        candidate_key=candidate.ref,
                    )
            continue
        selected[mbid] = fetched.popleft()
    return selected


async def _verify_files(
    context: RunContext,
    source: Source,
    candidate: Candidate,
    tracks: dict[str, Track],
    todo: dict[str, CandidateItem],
    selected: dict[str, Fetched],
    reliable: bool,
) -> dict[str, tuple[Fetched, Checked]]:
    if candidate.kind == "album" and isinstance(source, LocalSource | SoulseekSource):
        checked_files = await check_folder(
            {key: tracks[key] for key in todo},
            selected,
            reliable,
            context.deps.acoustid,
            context.deps.mb,
            context.cfg.acoustid_min_score,
            context.deps.tools,
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
                    context.deps.acoustid,
                    context.deps.mb,
                    context.cfg.acoustid_min_score,
                    context.deps.tools,
                ),
            )
    return checked_files


def _store_check(
    context: RunContext,
    source: str,
    candidate: Candidate,
    row: Work,
    file: Fetched,
    checked: Checked,
    mismatch: bool,
) -> tuple[bool, bool, bool] | None:
    with context.conn:
        if not context.change(
            row,
            State.FETCHED,
            path=str(file.path),
            source=source,
            codec=file.codec,
            bitrate=file.bitrate,
            lossless=file.lossless,
        ):
            return None
    row = replace(row, state=State.FETCHED)
    decision = checked.with_folder_guard(mismatch)
    retain = decision.outcome == "rejected_verify"
    wait_for_user = retain and row.pin is not None
    with context.conn:
        if decision.accept:
            changed = context.change(
                row,
                State.VERIFIED,
                verified_by=decision.verified_by,
                pinned_candidate=None,
                next_try_at=None,
            )
        else:
            changed = context.change(row, State.SEARCHING, path=None)
            if changed and wait_for_user:
                changed = context.change(
                    replace(row, state=State.SEARCHING),
                    State.MISSING,
                    next_try_at=None,
                )
        if changed and retain:
            review.reject(
                context.conn,
                row.id,
                source,
                candidate.ref,
                candidate.score,
                decision.reason,
                file,
                checked.comparison,
            )
        elif changed:
            queue.attempt(
                context.conn,
                row.id,
                source,
                file.item_ref,
                candidate.score,
                decision.outcome,
                decision.reason,
                candidate_key=candidate.ref,
            )
    return changed, decision.accept, wait_for_user


async def fetch(
    context: RunContext,
    source: Source,
    candidate: Candidate,
    todo: dict[str, CandidateItem],
    misses: dict[str, Work],
    tracks: dict[str, Track],
    reliable: bool,
) -> None:
    batch = await _acquire(context, source, candidate, todo, misses)
    if batch is None:
        return
    files, issues, wall = batch
    selected = _select_files(context, source.name, candidate, todo, misses, files, issues, wall)
    checked_files = await _verify_files(
        context, source, candidate, tracks, todo, selected, reliable
    )
    mismatch = any(checked.mismatch for _, checked in checked_files.values())
    for mbid, (file, checked) in checked_files.items():
        outcome = _store_check(
            context, source.name, candidate, misses[mbid], file, checked, mismatch
        )
        if outcome is None:
            continue
        changed, accepted, wait_for_user = outcome
        if accepted and changed and isinstance(source, LocalSource) and source.move:
            try:
                await asyncio.to_thread(Path(file.item_ref).unlink)
            except OSError as exc:
                log.warning("Cannot remove local original %s: %s", file.item_ref, exc)
        if accepted or wait_for_user or not changed:
            del misses[mbid]
    if wall is not None:
        raise wall


async def _download(context: RunContext, source: Source, candidate: Candidate) -> list[Fetched]:
    activity = (
        context.progress.downloading(source.name, candidate.title)
        if context.progress is not None
        else nullcontext()
    )
    with activity:
        return await source.fetch(candidate, context.workspace())
