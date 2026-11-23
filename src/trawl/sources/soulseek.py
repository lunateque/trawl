import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from functools import partial
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
_MIN_FILE_LIMIT = 200_000_000
_PCM_BYTES_PER_MS = 96_000 * 2 * 3 // 1000


def _abandon_stage(workspace: TemporaryDirectory[str], task: asyncio.Task[Fetched]) -> None:
    try:
        if not task.cancelled():
            task.exception()
        workspace.cleanup()
    except OSError as exc:
        _LOG.warning("Cannot clean up cancelled slskd staging files: %s", exc)


def _index_items(candidate: Candidate) -> tuple[dict[str, tuple[int, int]], dict[str, FetchIssue]]:
    indexed: dict[str, tuple[int, int]] = {}
    issues: dict[str, FetchIssue] = {}
    for n, item in enumerate(candidate.items):
        cap = max(_MIN_FILE_LIMIT, 3 * _PCM_BYTES_PER_MS * (item.expected_length_ms or 0))
        if item.size is None or item.size <= 0:
            issues[item.ref] = FetchIssue("slskd file has no valid size")
        elif item.size > cap:
            issues[item.ref] = FetchIssue(f"slskd file exceeds size limit ({cap} bytes)")
        else:
            indexed[item.ref] = (n, item.size)
    return indexed, issues


@dataclass
class _Waits:
    pending: set[str]
    ready: list[str]
    progress: dict[str, int]
    moves: dict[str, float]


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

    async def fetch(self, candidate: Candidate, into: Path) -> list[Fetched]:
        names = [str(self._destination(item.ref)).casefold() for item in candidate.items]
        if len(set(names)) != len(names):
            raise FetchError("slskd candidate has colliding download paths")
        endpoint = f"/transfers/downloads/{quote(candidate.ref, safe='')}"
        transfers: dict[str, str] = {}
        lock = self._peers.setdefault(candidate.ref, asyncio.Lock())
        deadline = asyncio.timeout(self._candidate_timeout)
        async with AsyncExitStack() as stack:
            try:
                async with deadline:
                    await stack.enter_async_context(lock)
                    # slskd flattens peer paths; another peer can write to the same destination.
                    for name in sorted(names):
                        await stack.enter_async_context(
                            self._paths.setdefault(name, asyncio.Lock())
                        )
                    async with self._limit:
                        return await self._fetch(candidate, into, endpoint, transfers)
            except TimeoutError:
                if not deadline.expired():
                    raise
                try:
                    async with asyncio.timeout(2):
                        for key in transfers.values():
                            await self._delete(f"{endpoint}/{quote(key, safe='')}", remove=True)
                except TimeoutError:
                    _LOG.warning("slskd transfer cleanup timed out")
                seconds = self._candidate_timeout
                raise FetchError(f"slskd candidate timed out after {seconds:g}s") from None

    async def _fetch(
        self,
        candidate: Candidate,
        into: Path,
        endpoint: str,
        transfer_ids: dict[str, str],
    ) -> list[Fetched]:
        indexed, issues = _index_items(candidate)
        if indexed:
            await self._api(
                "POST",
                endpoint,
                body=[{"filename": name, "size": size} for name, (_, size) in indexed.items()],
            )
        ready, wall = await self._poll(endpoint, indexed, transfer_ids, issues)
        files: list[Fetched] = []
        for name in ready:
            try:
                n, size = indexed[name]
                files.append(await self._stage(name, size, into, n))
            except (FetchError, AudioError, OSError) as exc:
                issues[name] = FetchIssue(str(exc), isinstance(exc, AudioError))
        if issues:
            raise PartialFetchError(files, issues, wall)
        return files

    async def _poll(
        self,
        endpoint: str,
        indexed: dict[str, tuple[int, int]],
        transfer_ids: dict[str, str],
        issues: dict[str, FetchIssue],
    ) -> tuple[list[str], SourceWall | None]:
        pending = set(indexed)
        waits = _Waits(pending, [], {}, dict.fromkeys(indexed, self._clock()))
        wall: SourceWall | None = None
        while pending:
            try:
                active = parse_transfers(await self._api("GET", endpoint))
            except (FetchError, SourceWall) as exc:
                if isinstance(exc, SourceWall):
                    wall = exc
                for name in pending:
                    issues[name] = FetchIssue(str(exc))
                break
            now = self._clock()
            stalled = False
            for name in sorted(pending):
                name_key = name.replace("\\", "/")
                stalled |= await self._poll_file(
                    endpoint,
                    name,
                    indexed[name][1],
                    active.get(name_key),
                    waits,
                    transfer_ids,
                    issues,
                    now,
                )
            if stalled:
                for transfer_id in transfer_ids.values():
                    await self._delete(f"{endpoint}/{quote(transfer_id, safe='')}", remove=True)
                issues.update(dict.fromkeys(indexed, FetchIssue("stalled")))
                raise PartialFetchError([], issues, None)
            if pending:
                await self._sleep(1)
        return waits.ready, wall

    async def _poll_file(
        self,
        endpoint: str,
        name: str,
        limit: int,
        transfer: Transfer | None,
        waits: _Waits,
        transfer_ids: dict[str, str],
        issues: dict[str, FetchIssue],
        now: float,
    ) -> bool:
        if transfer:
            transfer_ids[name] = transfer.id
            if transfer.bytes > limit:
                waits.pending.remove(name)
                issues[name] = FetchIssue("slskd transfer exceeded advertised size")
                await self._delete(f"{endpoint}/{quote(transfer.id, safe='')}", remove=True)
                del transfer_ids[name]
                return False
        if transfer and "completed" in transfer.state:
            waits.pending.remove(name)
            if "succeeded" in transfer.state:
                waits.ready.append(name)
            else:
                failure = ", ".join(sorted(transfer.state & _FAILED))
                issues[name] = FetchIssue(f"slskd transfer failed: {failure or 'not succeeded'}")
        else:
            if transfer and transfer.bytes > waits.progress.get(name, 0):
                waits.progress[name] = transfer.bytes
                waits.moves[name] = now
            return now - waits.moves[name] >= self._stall_timeout
        return False
