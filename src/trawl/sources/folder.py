import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from statistics import median

from trawl.mb.models import Release, Track
from trawl.normalize import norm_artist, norm_title
from trawl.sources.base import Candidate, CandidateItem
from trawl.sources.score import coverage, match_items, release_tracks, score, within_duration

TAGGABLE = frozenset({".flac", ".opus", ".ogg", ".m4a", ".mp3"})
REPACK_EXTS = frozenset({".wav", ".aiff", ".aif", ".ape", ".wv"})
AUDIO_EXTS = TAGGABLE | REPACK_EXTS
LOSSLESS_EXTS = REPACK_EXTS | {".flac"}
_DISC = re.compile(r"(?:cd|disc|disk|d)\s*([1-9][0-9]*)", re.I)
_NUMBER = re.compile(r"^(?:(\d{1,2})-(?=\d{2}\D))?(\d{1,3})[ ._)-]+(.+)$")


@dataclass(frozen=True)
class FolderFile:
    rel_path: PurePosixPath
    size: int
    length_ms: int | None
    bitrate: int | None
    ext: str
    ref: str | None = None
    disc: int | None = None
    lossless: bool | None = None

    @property
    def is_lossless(self) -> bool:
        return self.ext in LOSSLESS_EXTS if self.lossless is None else self.lossless

    def item(self) -> CandidateItem:
        stem = self.rel_path.stem
        parts = _NUMBER.match(stem)
        disc = self.disc if self.disc is not None else int(parts[1]) if parts and parts[1] else None
        title = parts[3] if parts else stem
        aliases = tuple(title.split(" - ")[1:])
        return CandidateItem(
            self.ref or self.rel_path.as_posix(),
            int(parts[2]) if parts else None,
            self.length_ms,
            title,
            disc,
            aliases,
            self.size,
        )


def disc_number(name: str) -> int | None:
    match = _DISC.fullmatch(name)
    return int(match[1]) if match else None


def group_disc_subfolders(files: Sequence[FolderFile]) -> list[FolderFile]:
    return [
        replace(file, disc=disc_number(file.rel_path.parent.name) or file.disc) for file in files
    ]


def pick_format_set(files: Sequence[FolderFile]) -> list[FolderFile]:
    audio = [file for file in files if file.ext in AUDIO_EXTS]
    sizes: dict[tuple[int | None, bool], list[int]] = {}
    for i, file in enumerate(audio):
        sizes.setdefault((file.disc, file.is_lossless), []).append(i)
    omitted: set[int] = set()
    for (disc, lossless), low in sizes.items():
        if lossless or not (high := sizes.get((disc, True))):
            continue
        stems = {audio[i].rel_path.stem.casefold() for i in high}
        omitted.update(i for i in low if audio[i].rel_path.stem.casefold() in stems)
        for ext in {audio[i].ext for i in low}:
            subset = [i for i in low if audio[i].ext == ext]
            if len(high) != len(subset):
                continue
            lengths = [audio[i].length_ms for i in high]
            other = [audio[i].length_ms for i in subset]
            if (
                None not in lengths
                and None not in other
                and all(
                    within_duration(a, b)
                    for a, b in zip(
                        sorted(lengths, key=lambda x: x or 0),
                        sorted(other, key=lambda x: x or 0),
                        strict=True,
                    )
                )
            ):
                omitted.update(subset)
    return [file for i, file in enumerate(audio) if i not in omitted]


def reject_cue_image(files: Sequence[FolderFile]) -> str | None:
    if sum(file.ext in AUDIO_EXTS for file in files) == 1 and any(
        file.ext == ".cue" for file in files
    ):
        return "cue image not supported"
    return None


def match_folder(
    release: Release, tracks: Sequence[Track], files: Sequence[FolderFile]
) -> dict[str, FolderFile]:
    by_item = {file.item(): file for file in files if file.ext in AUDIO_EXTS}
    return {key: by_item[item] for key, item in match_items(tracks, list(by_item), release).items()}


@dataclass(frozen=True)
class Folder:
    ref: str
    title: str
    files: tuple[FolderFile, ...]
    free_slot: bool = False
    upload_speed: int = 0


@dataclass(frozen=True)
class FolderMatch:
    release: Release
    candidate: Candidate
    mapping: dict[str, CandidateItem]
    reliable: bool


def rank_folders(
    source: str,
    folders: Sequence[Folder],
    editions: Sequence[tuple[Release, list[Track]]],
    *,
    album: bool,
    lossless_only: bool = False,
    min_coverage: float | None = None,
) -> list[FolderMatch]:
    threshold = min_coverage if min_coverage is not None else float(album)
    matches: list[FolderMatch] = []
    for folder in folders:
        if reject_cue_image(folder.files):
            continue
        files = pick_format_set(group_disc_subfolders(folder.files))
        if lossless_only:
            files = [file for file in files if file.is_lossless]
        for release, tracks in editions:
            matched = match_folder(release, tracks, files)
            single = len(files) == 1 and not album
            if single and source == "soulseek":
                file = files[0]
                matched = {}
                if file.length_ms is None:
                    continue
                name = norm_title(file.rel_path.stem.replace("_", " ").replace("-", " "))
                for track in tracks:
                    if (
                        any(
                            artist and artist in name
                            for credit in track.credits or release.credits
                            if (artist := norm_artist(credit.name))
                        )
                        and norm_title(track.title) in name
                        and within_duration(track.length_ms, file.length_ms)
                    ):
                        matched = {track.mbid: file}
                        break
                else:
                    matched = {}
            mapping = {key: file.item() for key, file in matched.items()}
            if not mapping or coverage(tracks, mapping) < threshold:
                continue
            chosen = list(matched.values())
            rates = [file.bitrate for file in chosen if file.bitrate is not None]
            candidate = Candidate(
                source,
                folder.ref,
                "track" if single else "album",
                folder.title,
                chosen[0].ext.removeprefix(".")
                if len({file.ext for file in chosen}) == 1
                else None,
                round(median(rates)) if rates else None,
                all(file.is_lossless for file in chosen),
                False,
                tuple(mapping.values()),
                free_slot=folder.free_slot,
                upload_speed=folder.upload_speed,
            )
            full = release_tracks(release)
            complete = not single and len(match_folder(release, full, files)) == len(full)
            matches.append(
                FolderMatch(
                    release,
                    replace(candidate, score=score(candidate, tracks, mapping)),
                    mapping,
                    complete,
                )
            )
    return sorted(
        matches,
        key=lambda match: (
            -match.candidate.score,
            -match.candidate.upload_speed,
            match.candidate.ref,
        ),
    )
