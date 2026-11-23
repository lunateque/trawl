import asyncio
import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import cast

import mutagen  # pyright: ignore[reportMissingTypeStubs]

from trawl.core.audio import AUDIO_EXTS, LOSSLESS
from trawl.core.catalog import Release, Track, credit_string
from trawl.core.fetch import Candidate, Fetched, FetchError, FetchIssue, PartialFetchError
from trawl.core.normalize import norm_artist, norm_title
from trawl.core.paths import is_link, linked_ancestor, unlink_regular
from trawl.library.repack import repack, repack_needed
from trawl.sources.folder import Folder, FolderFile, FolderMatch, disc_number, rank_folders
from trawl.verify.probe import AudioError, probe


def scan_folder(root: Path) -> list[tuple[FolderFile, Path]]:
    if linked_ancestor(root):
        return []
    rows: list[tuple[FolderFile, Path]] = []
    for entry in sorted(root.iterdir()):
        paths = (
            sorted(entry.iterdir())
            if not is_link(entry) and entry.is_dir() and disc_number(entry.name)
            else [entry]
        )
        for path in paths:
            ext = path.suffix.casefold()
            if is_link(path) or not path.is_file() or ext not in AUDIO_EXTS | {".cue"}:
                continue
            audio = None
            if ext in AUDIO_EXTS:
                try:
                    audio = probe(path)
                except AudioError:
                    continue
            relative = PurePosixPath(path.relative_to(root).as_posix())
            rows.append(
                (
                    FolderFile(
                        relative,
                        path.stat().st_size,
                        audio.duration_ms if audio else None,
                        audio.bitrate if audio else None,
                        ext,
                        str(path),
                        lossless=audio.codec in LOSSLESS or repack_needed(path) if audio else None,
                    ),
                    path,
                )
            )
    return rows


def _tags(path: Path) -> tuple[str, str]:
    try:
        raw = mutagen.File(path, easy=True)  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        tags = cast("dict[str, list[str]]", raw.tags or {}) if raw is not None else {}  # pyright: ignore[reportUnknownMemberType]
        artist = tags.get("albumartist") or tags.get("artist") or []
        album = tags.get("album") or []
        return artist[0] if artist else "", album[0] if album else ""
    except (mutagen.MutagenError, OSError):  # pyright: ignore[reportUnknownMemberType]
        return "", ""


def _name_matches(name: str, artist: str, title: str) -> bool:
    name = re.sub(r"[-_]", " ", name)
    return f" {artist} " in f" {norm_artist(name)} " and f" {title} " in f" {norm_title(name)} "


def _discover(inboxes: tuple[Path, ...], release: Release) -> list[Folder]:
    artist, title = norm_artist(credit_string(release.credits)), norm_title(release.title)
    if not artist or not title:
        return []
    folders: dict[Path, Folder] = {}
    for inbox in inboxes:
        if linked_ancestor(inbox) or not inbox.is_dir():
            continue
        for root in sorted(inbox.resolve().iterdir()):
            if is_link(root) or not root.is_dir() or root in folders:
                continue
            files = scan_folder(root)
            if not _name_matches(root.name, artist, title):
                for file, path in files:
                    if file.ext in AUDIO_EXTS:
                        artist_tag, album_tag = _tags(path)
                        if norm_artist(artist_tag) == artist and norm_title(album_tag) == title:
                            break
                else:
                    continue
            folders[root] = Folder(str(root), root.name, tuple(file for file, _ in files))
    return list(folders.values())


class LocalSource:
    name = "local"

    def __init__(
        self, inbox: tuple[Path, ...], *, move: bool = False, lossless_repack: bool = True
    ) -> None:
        self.inbox = inbox
        self.move = move
        self.lossless_repack = lossless_repack

    async def search(self, release: Release, tracks: list[Track]) -> list[Candidate]:
        return [
            found.candidate for found in await self.find(release, [(release, tracks)], album=False)
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
        folders = await asyncio.to_thread(_discover, self.inbox, release)
        return rank_folders(
            self.name,
            folders,
            editions,
            album=album,
            lossless_only=lossless_only,
            min_coverage=min_coverage,
        )

    async def fetch(self, candidate: Candidate, into: Path) -> list[Fetched]:
        return await asyncio.to_thread(self._fetch, candidate, into)

    def _fetch(self, candidate: Candidate, into: Path) -> list[Fetched]:
        root = Path(candidate.ref)
        files: list[Fetched] = []
        issues: dict[str, FetchIssue] = {}
        for n, item in enumerate(candidate.items):
            path = Path(item.ref)
            try:
                if linked_ancestor(path) or not path.resolve().is_relative_to(root.resolve()):
                    raise FetchError("local file is outside the selected folder")
                files.append(
                    stage_file(
                        item.ref,
                        path,
                        into,
                        n,
                        move=self.move,
                        lossless_repack=self.lossless_repack,
                    )
                )
            except (OSError, AudioError, FetchError) as exc:
                issues[item.ref] = FetchIssue(str(exc), isinstance(exc, AudioError))
        if issues:
            raise PartialFetchError(files, issues, None)
        return files


def stage_file(
    ref: str, src: Path, into: Path, n: int, *, move: bool, lossless_repack: bool
) -> Fetched:
    from shutil import copyfile

    if src.suffix.casefold() not in AUDIO_EXTS:
        raise AudioError("unsupported audio format")
    if repack_needed(src):
        if not lossless_repack:
            raise AudioError("lossless repack is disabled")
        dst = into / f"{n}.flac"
        repack(src, dst)
    else:
        dst = into / f"{n}{src.suffix.casefold()}"
        copyfile(src, dst)
    audio = probe(dst)
    if move:
        unlink_regular(src)
    return Fetched(ref, dst, audio.codec, audio.bitrate, audio.codec in LOSSLESS)
