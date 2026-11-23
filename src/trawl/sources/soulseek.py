import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field, replace
from functools import partial
from os.path import lexists
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import quote
from uuid import uuid4
from weakref import WeakValueDictionary

from trawl.config.secrets import slskd_insecure
from trawl.core.catalog import Release, Track, credit_string, object_value
from trawl.core.fetch import (
    Candidate,
    Fetched,
    FetchError,
    FetchIssue,
    PartialFetchError,
    SourceWall,
)
from trawl.core.paths import linked_ancestor, unlink_regular
from trawl.net.client import HttpClient, HttpError
from trawl.sources.folder import Folder, FolderMatch, rank_folders
from trawl.sources.local import stage_file
from trawl.sources.slskd import (
    Transfer,
    index_items,
    is_path_component,
    parse_folders,
    parse_transfers,
    remote_path,
)
from trawl.verify.probe import AudioError

_LOG = logging.getLogger(__name__)
_EDITION = re.compile(
    r"\s+(?:-\s+|\()(?:(?:\d{4} )?remaster(?:ed)?(?: \d{4})?(?: version)?"
    r"|(?:deluxe|expanded|special|anniversary)(?: edition)?|\d+th anniversary(?: edition)?)\)?$",
    re.I,
)
_FAILED = {"errored", "rejected", "cancelled", "timedout", "timed out"}


def _abandon_stage(workspace: TemporaryDirectory[str], task: asyncio.Task[Fetched]) -> None:
    try:
        if not task.cancelled():
            task.exception()
        workspace.cleanup()
    except OSError as exc:
        _LOG.warning("Cannot clean up cancelled slskd staging files: %s", exc)


@dataclass
class _Waits:
    pending: set[str]
    progress: dict[str, int]
    moves: dict[str, float]


@dataclass
class _Downloads:
    previous: set[str] = field(default_factory=set[str])
    preexisting: set[str] = field(default_factory=set[str])
    ids: dict[str, str] = field(default_factory=dict[str, str])
    ready: set[str] = field(default_factory=set[str])


class SoulseekSource:
    name = "soulseek"

    def __init__(
        self,
        http: HttpClient,
        url: str,
        key: str | None,
        download_dir: Path,
        *,
        concurrency: int = 3,
        search_timeout_s: int = 30,
        stall_timeout_s: int = 600,
        candidate_timeout_s: float = 3600,
        allow_insecure: bool = False,
        lossless_repack: bool = True,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if slskd_insecure(url) and not allow_insecure:
            raise ValueError("slskd requires HTTPS for non-loopback hosts")
        self._http, self._base, self._key = http, url.rstrip("/") + "/api/v0", key
        self._download = download_dir.resolve()
        self._limit = asyncio.Semaphore(concurrency)
        self._peers: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._paths: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._search_timeout, self._stall_timeout = search_timeout_s, stall_timeout_s
        self._candidate_timeout = candidate_timeout_s
        self._clock, self._sleep = clock, sleep
        self._repack = lossless_repack

    async def _api(
        self,
        method: str,
        endpoint: str,
        *,
        body: object = None,
        params: Mapping[str, str] | None = None,
        missing_ok: bool = False,
    ) -> object:
        if not self._key:
            raise SourceWall("slskd_api_key is not configured")
        try:
            return await self._http.api_json(
                method,
                self._base + endpoint,
                headers={"X-API-Key": self._key},
                body=body,
                params=params,
                service="slskd",
            )
        except HttpError as exc:
            if missing_ok and exc.status == 404:
                return []
            if exc.status is None or exc.status in {401, 403, 429} or exc.status >= 500:
                raise SourceWall(str(exc)) from None
            raise FetchError(str(exc)) from None

    async def check(self) -> None:
        await self._api("GET", "/application")

    async def _delete(self, endpoint: str, *, remove: bool = False) -> None:
        try:
            await self._api("DELETE", endpoint, params={"remove": "true"} if remove else None)
        except (FetchError, SourceWall) as exc:
            _LOG.warning("Cannot clean up slskd %s: %s", endpoint, exc)

    async def _search(self, query: str) -> list[Folder]:
        key = str(uuid4())
        endpoint = f"/searches/{key}"
        await self._api("POST", "/searches", body={"id": key, "searchText": query})
        try:
            deadline = self._clock() + self._search_timeout
            while True:
                status = object_value(await self._api("GET", endpoint))
                left = deadline - self._clock()
                if status.get("isComplete") is True or left <= 0:
                    break
                await self._sleep(min(1, left))
            return parse_folders(await self._api("GET", f"{endpoint}/responses"))
        finally:
            await self._delete(endpoint)

    async def search(self, release: Release, tracks: list[Track]) -> list[Candidate]:
        return [
            match.candidate for match in await self.find(release, [(release, tracks)], album=False)
        ]

    async def find(
        self,
        release: Release,
        editions: Sequence[tuple[Release, list[Track]]],
        *,
        album: bool,
        lossless_only: bool = False,
        min_coverage: float | None = None,
    ) -> list[FolderMatch]:
        artist = credit_string(release.credits)
        title = release.title.strip()
        queries = [f"{artist} {title}", f"{artist} {_EDITION.sub('', title)}"]
        if len(title.split()) >= 2:
            queries.append(title)
        if not album:
            queries.extend(
                f"{credit_string(track.credits) or artist} {track.title}"
                for track in editions[0][1]
            )
        for query in dict.fromkeys(query.strip() for query in queries if query.strip()):
            output = rank_folders(
                self.name,
                await self._search(query),
                editions,
                album=album,
                lossless_only=lossless_only,
                min_coverage=min_coverage,
            )
            if output:
                return output
        return []

    def _destination(self, remote: str) -> Path:
        path = remote_path(remote)
        if path is None or any(
            not is_path_component(part) for part in (path.parent.name, path.name)
        ):
            raise FetchError("unsafe slskd download path")
        return self._download / path.parent.name / path.name

    def _path(self, remote: str, expected: int) -> Path:
        dst = self._destination(remote)
        if linked_ancestor(dst) or not dst.resolve().is_relative_to(self._download):
            raise FetchError("slskd file is outside the download directory")
        if not dst.is_file() or dst.stat().st_size != expected:
            raise FetchError(f"slskd completed file missing or wrong size: {dst.name}")
        return dst

    async def _stage(self, remote: str, size: int, into: Path, index: int) -> Fetched:
        source = self._path(remote, size)
        await asyncio.to_thread(into.mkdir, parents=True, exist_ok=True)
        # An executor thread cannot be cancelled mid-copy. Publish only after it succeeds.
        workspace = TemporaryDirectory(dir=into.parent)
        task = asyncio.create_task(
            asyncio.to_thread(
                stage_file,
                remote,
                source,
                Path(workspace.name),
                index,
                move=False,
                lossless_repack=self._repack,
            )
        )
        abandoned = False
        try:
            file = await asyncio.shield(task)
            final = into / file.path.name
            file.path.replace(final)
            unlink_regular(source)
            return replace(file, path=final)
        except asyncio.CancelledError:
            abandoned = True
            task.add_done_callback(partial(_abandon_stage, workspace))
            raise
        finally:
            if not abandoned:
                workspace.cleanup()

    def _discard(self, ready: set[str], indexed: dict[str, tuple[int, int]]) -> None:
        for name in ready:
            try:
                path = self._path(name, indexed[name][1])
                if unlink_regular(path):
                    with suppress(OSError):
                        path.parent.rmdir()
            except (FetchError, OSError) as exc:
                _LOG.warning("Cannot clean up rejected slskd file: %s", exc)

    async def _cancel(self, endpoint: str, downloads: _Downloads) -> None:
        for key in downloads.ids.values():
            await self._delete(f"{endpoint}/{quote(key, safe='')}", remove=True)

    async def fetch(self, candidate: Candidate, into: Path) -> list[Fetched]:
        names = [str(self._destination(item.ref)).casefold() for item in candidate.items]
        if len(set(names)) != len(names):
            raise FetchError("slskd candidate has colliding download paths")
        endpoint = f"/transfers/downloads/{quote(candidate.ref, safe='')}"
        indexed, issues = index_items(candidate)
        downloads = _Downloads()
        deadline = asyncio.timeout(self._candidate_timeout)
        async with AsyncExitStack() as stack:
            try:
                async with deadline:
                    peer = self._peers.setdefault(candidate.ref, asyncio.Lock())
                    await stack.enter_async_context(peer)
                    # slskd flattens peer paths; another peer can write to the same destination.
                    for name in sorted(names):
                        await stack.enter_async_context(
                            self._paths.setdefault(name, asyncio.Lock())
                        )
                    async with self._limit:
                        return await self._fetch(into, endpoint, indexed, issues, downloads)
            except (TimeoutError, asyncio.CancelledError) as exc:
                if isinstance(exc, TimeoutError) and not deadline.expired():
                    raise
                try:
                    async with asyncio.timeout(2):
                        await self._cancel(endpoint, downloads)
                except TimeoutError:
                    _LOG.warning("slskd transfer cleanup timed out")
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise FetchError(
                    f"slskd candidate timed out after {self._candidate_timeout:g}s"
                ) from None
            finally:
                if discarded := downloads.ready - downloads.preexisting:
                    await asyncio.to_thread(self._discard, discarded, indexed)

    async def _fetch(
        self,
        into: Path,
        endpoint: str,
        indexed: dict[str, tuple[int, int]],
        issues: dict[str, FetchIssue],
        downloads: _Downloads,
    ) -> list[Fetched]:
        if indexed:
            # The enqueue response has no transfer IDs. Exclude old records and files.
            prior = await self._api("GET", endpoint, missing_ok=True)
            downloads.previous = {transfer.id for _, transfer in parse_transfers(prior)}
            downloads.preexisting = await asyncio.to_thread(
                lambda: {name for name in indexed if lexists(self._destination(name))}
            )
            await self._api(
                "POST",
                endpoint,
                body=[{"filename": name, "size": size} for name, (_, size) in indexed.items()],
            )
        wall = await self._poll(endpoint, indexed, downloads, issues)
        files: list[Fetched] = []
        for name in sorted(downloads.ready):
            try:
                n, size = indexed[name]
                files.append(await self._stage(name, size, into, n))
            except (FetchError, AudioError, OSError) as exc:
                issues[name] = FetchIssue(str(exc), isinstance(exc, AudioError))
            else:
                downloads.ready.remove(name)
        if issues:
            raise PartialFetchError(files, issues, wall)
        return files

    async def _poll(
        self,
        endpoint: str,
        indexed: dict[str, tuple[int, int]],
        downloads: _Downloads,
        issues: dict[str, FetchIssue],
    ) -> SourceWall | None:
        pending = set(indexed)
        waits = _Waits(pending, {}, dict.fromkeys(indexed, self._clock()))
        wall: SourceWall | None = None
        while pending:
            try:
                active = {
                    name: transfer
                    for name, transfer in parse_transfers(await self._api("GET", endpoint))
                    if transfer.id not in downloads.previous
                }
            except (FetchError, SourceWall) as exc:
                if isinstance(exc, SourceWall):
                    wall = exc
                for name in pending:
                    issues[name] = FetchIssue(str(exc))
                break
            now = self._clock()
            stalled = False
            for name in sorted(pending):
                stalled |= await self._poll_file(
                    endpoint,
                    name,
                    indexed[name][1],
                    active.get(name.replace("\\", "/")),
                    waits,
                    downloads,
                    issues,
                    now,
                )
            if stalled:
                await self._cancel(endpoint, downloads)
                issues.update(dict.fromkeys(indexed, FetchIssue("stalled")))
                raise PartialFetchError([], issues, None)
            if pending:
                await self._sleep(1)
        return wall

    async def _poll_file(
        self,
        endpoint: str,
        name: str,
        limit: int,
        transfer: Transfer | None,
        waits: _Waits,
        downloads: _Downloads,
        issues: dict[str, FetchIssue],
        now: float,
    ) -> bool:
        if transfer:
            downloads.ids[name] = transfer.id
            if transfer.bytes > limit:
                waits.pending.remove(name)
                issues[name] = FetchIssue("slskd transfer exceeded advertised size")
                await self._delete(f"{endpoint}/{quote(transfer.id, safe='')}", remove=True)
                del downloads.ids[name]
                return False
        if transfer and "completed" in transfer.state:
            waits.pending.remove(name)
            if "succeeded" in transfer.state:
                downloads.ready.add(name)
            else:
                failure = ", ".join(sorted(transfer.state & _FAILED))
                issues[name] = FetchIssue(f"slskd transfer failed: {failure or 'not succeeded'}")
        else:
            if transfer and transfer.bytes > waits.progress.get(name, 0):
                waits.progress[name] = transfer.bytes
                waits.moves[name] = now
            return now - waits.moves[name] >= self._stall_timeout
        return False
