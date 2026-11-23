import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from time import monotonic
from typing import cast
from urllib.parse import quote
from uuid import uuid4
from weakref import WeakValueDictionary

from trawl.config import slskd_insecure
from trawl.library.place import linked_ancestor, unlink_regular
from trawl.mb.models import Release, Track, array_value, credit_string, object_value
from trawl.net.client import HttpClient, HttpError
from trawl.sources.base import (
    Candidate,
    Fetched,
    FetchError,
    FetchIssue,
    PartialFetchError,
    SourceWall,
)
from trawl.sources.folder import (
    AUDIO_EXTS,
    Folder,
    FolderFile,
    FolderMatch,
    disc_number,
    rank_folders,
)
from trawl.sources.local import stage_file
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


def _number(raw: object) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None


def _remote(raw: str) -> PurePosixPath | None:
    path = PurePosixPath(raw.replace("\\", "/"))
    if not path.name or ".." in path.parts or len(path.parts) < 2:
        return None
    return path


def _folders(response: object) -> list[Folder]:
    groups: dict[tuple[str, PurePosixPath], list[FolderFile]] = {}
    stats: dict[str, tuple[bool, int]] = {}
    for raw in array_value(response):
        peer = object_value(raw)
        user = peer.get("username")
        if not isinstance(user, str) or not user:
            continue
        stats[user] = (peer.get("hasFreeUploadSlot") is True, _number(peer.get("uploadSpeed")) or 0)
        locked = {
            object_value(item).get("filename")
            for item in array_value(peer.get("lockedFiles"))
            if isinstance(object_value(item).get("filename"), str)
        }
        for value in array_value(peer.get("files")):
            file = object_value(value)
            name = file.get("filename")
            size = _number(file.get("size"))
            if (
                not isinstance(name, str)
                or name in locked
                or (path := _remote(name)) is None
                or size is None
            ):
                continue
            ext = path.suffix.casefold()
            if ext not in AUDIO_EXTS | {".cue"}:
                continue
            root = path.parent.parent if disc_number(path.parent.name) else path.parent
            seconds = _number(file.get("length"))
            groups.setdefault((user, root), []).append(
                FolderFile(
                    path.relative_to(root),
                    size,
                    seconds * 1000 if seconds is not None else None,
                    _number(file.get("bitRate")),
                    ext,
                    name,
                )
            )
    return [
        Folder(user, str(root), tuple(dict.fromkeys(files)), *stats[user])
        for (user, root), files in groups.items()
    ]


@dataclass(frozen=True)
class _Transfer:
    id: str
    bytes: int
    state: frozenset[str]


def _transfers(raw: object) -> dict[str, _Transfer]:
    transfers: dict[str, _Transfer] = {}
    stack = [raw]
    while stack:
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(reversed(cast("list[object]", value)))
            continue
        if not isinstance(value, dict):
            continue
        info = cast("dict[str, object]", value)
        if "filename" not in info:
            stack.extend(info.get(key) for key in ("files", "directories", "downloads"))
            continue
        name, key, state = info.get("filename"), info.get("id"), info.get("state")
        if isinstance(name, str) and isinstance(key, str) and isinstance(state, str):
            transfers[name.replace("\\", "/")] = _Transfer(
                key,
                _number(info.get("bytesTransferred")) or 0,
                frozenset(word.strip().casefold() for word in state.split(",")),
            )
    return transfers


def _component(value: str) -> bool:
    return bool(
        value
        and not value.endswith((" ", "."))
        and not re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
        and not PureWindowsPath(value).is_reserved()
    )


def _abandon_stage(workspace: TemporaryDirectory[str], task: asyncio.Task[Fetched]) -> None:
    try:
        if not task.cancelled():
            task.exception()
        workspace.cleanup()
    except OSError as exc:
        _LOG.warning("Cannot clean up cancelled slskd staging files: %s", exc)


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
            return _folders(await self._api("GET", f"{endpoint}/responses"))
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
        path = _remote(remote)
        if path is None or any(not _component(part) for part in (path.parent.name, path.name)):
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
        if indexed:
            await self._api(
                "POST",
                endpoint,
                body=[{"filename": name, "size": size} for name, (_, size) in indexed.items()],
            )
        pending = set(indexed)
        ready: list[str] = []
        progress: dict[str, int] = {}
        moves: dict[str, float] = dict.fromkeys(indexed, self._clock())
        wall: SourceWall | None = None
        while pending:
            try:
                active = _transfers(await self._api("GET", endpoint))
            except (FetchError, SourceWall) as exc:
                if isinstance(exc, SourceWall):
                    wall = exc
                for name in pending:
                    issues[name] = FetchIssue(str(exc))
                break
            now = self._clock()
            stalled = False
            for name in sorted(pending):
                transfer = active.get(name.replace("\\", "/"))
                if transfer:
                    transfer_ids[name] = transfer.id
                    if transfer.bytes > indexed[name][1]:
                        pending.remove(name)
                        issues[name] = FetchIssue("slskd transfer exceeded advertised size")
                        await self._delete(f"{endpoint}/{quote(transfer.id, safe='')}", remove=True)
                        del transfer_ids[name]
                        continue
                if transfer and "completed" in transfer.state:
                    pending.remove(name)
                    if "succeeded" in transfer.state:
                        ready.append(name)
                    else:
                        failure = ", ".join(sorted(transfer.state & _FAILED))
                        issues[name] = FetchIssue(
                            f"slskd transfer failed: {failure or 'not succeeded'}"
                        )
                else:
                    if transfer and transfer.bytes > progress.get(name, 0):
                        progress[name] = transfer.bytes
                        moves[name] = now
                    stalled |= now - moves[name] >= self._stall_timeout
            if stalled:
                for transfer_id in transfer_ids.values():
                    await self._delete(f"{endpoint}/{quote(transfer_id, safe='')}", remove=True)
                issues.update(dict.fromkeys(indexed, FetchIssue("stalled")))
                raise PartialFetchError([], issues, None)
            if pending:
                await self._sleep(1)
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
