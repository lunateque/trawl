from dataclasses import replace

from trawl.core.catalog import MbDataError, Release, Track, release_tracks
from trawl.core.fetch import (
    Candidate,
    CandidateItem,
    FetchError,
    SourceWall,
)
from trawl.db import queue, review
from trawl.db.queue import Work
from trawl.plan.import_folder import imported_folder
from trawl.runner.context import RunContext
from trawl.runner.editions import folder_candidates
from trawl.runner.fetch import fetch
from trawl.sources.base import Source
from trawl.sources.folder import FolderMatch
from trawl.sources.local import LocalSource
from trawl.sources.score import coverage, match_items, score
from trawl.sources.soulseek import SoulseekSource


async def _candidate_search(
    context: RunContext,
    source: Source,
    release: Release,
    basis: list[Track],
    album: bool,
) -> list[tuple[Candidate, dict[str, CandidateItem], bool]]:
    threshold = context.cfg.sources.album_coverage if album else context.cfg.sources.track_coverage
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


async def _search_imports(
    context: RunContext,
    imports: list[FolderMatch],
    tracks: dict[str, Track],
    misses: dict[str, Work],
    tried: set[str],
) -> None:
    for imported in imports:
        if context.stopping():
            return
        source = LocalSource(
            (),
            move=context.cfg.sources.local.move,
            lossless_repack=context.cfg.sources.lossless_repack,
        )
        todo = {key: item for key, item in imported.mapping.items() if key in misses}
        if todo:
            tried.update(todo)
            _remember(context, [(imported.candidate, todo, imported.reliable)], misses)
            await fetch(
                context, source, imported.candidate, todo, misses, tracks, imported.reliable
            )
        if not misses:
            return


async def _rank_source(
    context: RunContext,
    source: Source,
    release: Release,
    basis: list[Track],
    misses: dict[str, Work],
    album: bool,
    allow_switch: bool,
) -> tuple[Release | None, list[tuple[Candidate, dict[str, CandidateItem], bool]]]:
    if isinstance(source, LocalSource | SoulseekSource):
        selected, ranked = await folder_candidates(
            context, source, release, basis, misses, album, allow_switch
        )
        if selected:
            return selected, []
    elif source.name == "ytmusic":
        async with context.youtube:
            ranked = await _candidate_search(context, source, release, basis, album)
    else:
        ranked = await _candidate_search(context, source, release, basis, album)
    return None, ranked


async def _try_ranked(
    context: RunContext,
    source: Source,
    ranked: list[tuple[Candidate, dict[str, CandidateItem], bool]],
    tracks: dict[str, Track],
    misses: dict[str, Work],
    tried: set[str],
    delayed: set[str],
) -> None:
    _remember(context, ranked, misses)
    for candidate, mapping, reliable in ranked:
        if context.stopping():
            return
        if until := queue.pause_until(context.conn, source.name):
            delayed.add(until)
            break
        todo = {mbid: item for mbid, item in mapping.items() if mbid in misses}
        if not todo:
            continue
        tried.update(todo)
        await fetch(context, source, candidate, todo, misses, tracks, reliable)
        if not misses:
            return


def _finish_search(
    context: RunContext, misses: dict[str, Work], tried: set[str], delayed: set[str]
) -> None:
    if context.stopping():
        return
    if delayed:
        context.requeue(misses, min(delayed))
        return
    for mbid, row in misses.items():
        if mbid not in tried:
            with context.conn:
                queue.attempt(
                    context.conn,
                    row.id,
                    context.deps.sources[-1].name if context.deps.sources else "local",
                    None,
                    None,
                    "no_candidates",
                    "no eligible source candidates",
                )
        context.missing(row)


async def search(
    context: RunContext,
    release: Release,
    tracks: dict[str, Track],
    misses: dict[str, Work],
    *,
    allow_switch: bool,
) -> Release | None:
    delayed: set[str] = set()
    tried: set[str] = set()
    with context.conn:
        for row in misses.values():
            review.clear_candidates(context.conn, row.id)
    imports = imported_folder(context.conn, release)
    await _search_imports(context, imports, tracks, misses, tried)
    if context.stopping() or not misses:
        return None
    for source in context.deps.sources:
        if context.stopping():
            return None
        if until := queue.pause_until(context.conn, source.name):
            delayed.add(until)
            continue
        targets = [tracks[mbid] for mbid in misses]
        album = any(row.album_scope for row in misses.values())
        basis = release_tracks(release) if album else targets
        try:
            selected, ranked = await _rank_source(
                context, source, release, basis, misses, album, allow_switch and not imports
            )
            if selected:
                return selected
            await _try_ranked(context, source, ranked, tracks, misses, tried, delayed)
            if not misses:
                return None
        except SourceWall as exc:
            with context.conn:
                until = queue.wall(context.conn, source.name, exc.reason)
                for row in misses.values():
                    queue.attempt(
                        context.conn, row.id, source.name, None, None, "fetch_failed", exc.reason
                    )
            delayed.add(until)
        except (FetchError, MbDataError, OSError) as exc:
            with context.conn:
                for mbid, row in misses.items():
                    tried.add(mbid)
                    queue.attempt(
                        context.conn, row.id, source.name, None, None, "fetch_failed", str(exc)
                    )
    _finish_search(context, misses, tried, delayed)
    return None


def _remember(
    context: RunContext,
    ranked: list[tuple[Candidate, dict[str, CandidateItem], bool]],
    misses: dict[str, Work],
) -> None:
    with context.conn:
        for candidate, mapping, _ in ranked:
            for mbid, item in mapping.items():
                if (row := misses.get(mbid)) is not None:
                    review.remember(context.conn, row.id, candidate, item)
