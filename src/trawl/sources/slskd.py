import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import cast

from trawl.core.audio import AUDIO_EXTS
from trawl.core.catalog import array_value, object_value
from trawl.core.fetch import Candidate, FetchIssue
from trawl.sources.folder import Folder, FolderFile, disc_number

_MIN_FILE_LIMIT = 200_000_000
_PCM_BYTES_PER_MS = 96_000 * 2 * 3 // 1000


def index_items(candidate: Candidate) -> tuple[dict[str, tuple[int, int]], dict[str, FetchIssue]]:
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


def _number(raw: object) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None


def remote_path(raw: str) -> PurePosixPath | None:
    path = PurePosixPath(raw.replace("\\", "/"))
    if not path.name or ".." in path.parts or len(path.parts) < 2:
        return None
    return path


def parse_folders(response: object) -> list[Folder]:
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
                or (path := remote_path(name)) is None
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
class Transfer:
    id: str
    bytes: int
    state: frozenset[str]


def parse_transfers(raw: object) -> Iterator[tuple[str, Transfer]]:
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
            yield (
                name.replace("\\", "/"),
                Transfer(
                    key,
                    _number(info.get("bytesTransferred")) or 0,
                    frozenset(word.strip().casefold() for word in state.split(",")),
                ),
            )


def is_path_component(value: str) -> bool:
    return bool(
        value
        and not value.endswith((" ", "."))
        and not re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
        and not PureWindowsPath(value).is_reserved()
    )
