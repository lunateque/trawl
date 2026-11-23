import asyncio
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, cast

from yt_dlp import YoutubeDL  # pyright: ignore[reportMissingTypeStubs]
from yt_dlp.postprocessor.ffmpeg import (  # pyright: ignore[reportMissingTypeStubs]
    FFmpegExtractAudioPP,
)
from yt_dlp.utils import (  # pyright: ignore[reportMissingTypeStubs]
    DownloadError,
    ExtractorError,
    PostProcessingError,
)
from ytmusicapi import YTMusic  # pyright: ignore[reportMissingTypeStubs]
from ytmusicapi.exceptions import YTMusicError  # pyright: ignore[reportMissingTypeStubs]

from trawl.core.audio import LOSSLESS, within_duration
from trawl.core.catalog import (
    JsonObject,
    Release,
    Track,
    array_value,
    credit_string,
    object_value,
    release_tracks,
)
from trawl.core.fetch import (
    Candidate,
    CandidateItem,
    Fetched,
    FetchError,
    FetchIssue,
    PartialFetchError,
    SourceWall,
)
from trawl.core.normalize import norm_artist, norm_title
from trawl.sources.score import match_items
from trawl.verify.probe import AudioError, command, probe

_WALL = re.compile(
    r"\bHTTP(?: Error| status(?: code)?)?[: ]+(?:403|429)\b"
    r"|\b(?:403|429) Client Error\b|sign in to confirm|not a bot",
    re.I,
)
_STATUS = re.compile(r"\b(?:HTTP(?: Error| status(?: code)?)?[: ]+(\d{3})|(\d{3}) Client Error)\b")
_SEARCH_URL = "https://music.youtube.com/youtubei/v1/search"
_VIDEO_URL = "https://www.youtube.com/watch"
_DASH = re.compile(r"\s+[-\u2013\u2014]\s+")
_BRACKET = re.compile(r"\s+(?:\(([^()]*)\)|\[([^\[\]]*)\])$")
_FEATURE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.+$", re.I)
_PRESENTATION = re.compile(
    r"(?:official )?(?:audio|(?:music |lyrics? )?video)"
    r"|(?:\d{4} )?remaster(?:ed)?(?: \d{4})?(?: version)?",
    re.I,
)
_FEATURE_WORDS = ("feat ", "ft ", "featuring ")
_DIFFERENT_VERSION = re.compile(
    r"\b(?:live|remix(?:ed)?|cover|karaoke|instrumental|sped[\s-]+up|slowed|nightcore|8d|acoustic)\b",
    re.I,
)


def is_wall(exc: Exception) -> bool:
    return _WALL.search(str(exc)) is not None


def _diagnostic(exc: Exception, url: str) -> str:
    match = _STATUS.search(str(exc))
    status = f" HTTP {match[1] or match[2]}" if match else ""
    reason = " (sign-in or rate limit required)" if is_wall(exc) else ""
    return f"YouTube Music{status} {type(exc).__name__} at {url}{reason}"


class Downloader(Protocol):
    def download(self, video_id: str, into: Path) -> Path: ...


class CopyAudio(FFmpegExtractAudioPP):
    def get_audio_codec(self, path: str) -> str:
        return probe(Path(path)).codec

    def run_ffmpeg(self, path: str, out_path: str, codec: str, more_opts: list[str]) -> None:
        if codec != "copy":
            raise PostProcessingError("audio cannot be extracted without re-encoding")
        result = command(
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            path,
            "-vn",
            "-c:a",
            codec,
            *more_opts,
            out_path,
        )
        if result.returncode:
            raise AudioError("ffmpeg could not extract audio")


class _QuietDownloaderLog:
    def debug(self, _: str) -> None:
        pass

    info = warning = error = debug


class YtDlpDownloader:
    def __init__(self, cookies: Path | None = None) -> None:
        self._cookies = cookies

    def download(self, video_id: str, into: Path) -> Path:
        options: Any = {
            "format": "bestaudio",
            "outtmpl": str(into / "audio.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "cachedir": False,
            "logger": _QuietDownloaderLog(),
        }
        if self._cookies is not None:
            options["cookiefile"] = str(self._cookies)
        with YoutubeDL(options) as ydl:  # pyright: ignore[reportUnknownVariableType]
            processor = CopyAudio(ydl, preferredcodec="best")
            ydl.add_post_processor(processor)  # pyright: ignore[reportUnknownMemberType,reportArgumentType]
            result: object = ydl.extract_info(f"{_VIDEO_URL}?v={video_id}")  # pyright: ignore[reportUnknownMemberType]
        data = object_value(result)
        if "requested_downloads" in data:
            downloads = array_value(data["requested_downloads"])
            path = object_value(downloads[0]).get("filepath") if downloads else None
        else:
            path = data.get("filepath")
        if not isinstance(path, str) or not Path(path).is_file():
            raise FetchError("yt-dlp did not return an extracted audio file")
        return Path(path)


def _artist_names(data: JsonObject) -> set[str]:
    return {
        norm_artist(name.casefold().removesuffix(" - topic"))
        for raw in array_value(data.get("artists"))
        if isinstance(name := object_value(raw).get("name"), str)
    }


def _item(data: JsonObject) -> CandidateItem | None:
    ref, title = data.get("videoId"), data.get("title")
    if not isinstance(ref, str) or not ref or not isinstance(title, str):
        return None
    seconds = data.get("duration_seconds")
    number = data.get("trackNumber")
    return CandidateItem(
        ref,
        number if isinstance(number, int) else None,
        seconds * 1000 if isinstance(seconds, int) else None,
        title,
    )


def _candidate(
    ref: str, title: str, items: tuple[CandidateItem, ...], *, album: bool, official: bool
) -> Candidate:
    return Candidate(
        "ytmusic", ref, "album" if album else "track", title, None, None, False, official, items
    )


def _qualifier(text: str, album: str) -> bool:
    label = norm_title(text)
    return (
        label in {"rough trade version", f"{album} ep", f"{album} bside"}
        or label.startswith(_FEATURE_WORDS)
        or _PRESENTATION.fullmatch(text) is not None
    )


def _single_title(title: str, album: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    unknown: list[str] = []
    versions: list[str] = []
    title = title.strip()
    while title:
        if bracket := _BRACKET.search(title):
            suffix = _DASH.split(bracket[1] or bracket[2] or "")
            title = title[: bracket.start()]
        elif len(parts := _DASH.split(title)) > 1:
            suffix = parts[-1:]
            title = title[: len(title) - len(parts[-1])].rstrip(" -\u2013\u2014")
        else:
            if (feature := _FEATURE.search(title)) and not any(ch in feature[0] for ch in "()[]"):
                title = title[: feature.start()]
            break
        for part in reversed(suffix):
            if _DIFFERENT_VERSION.search(part):
                versions.append(norm_title(part))
            elif not _qualifier(part, album):
                unknown.append(norm_title(part))
    return norm_title(title), tuple(reversed(versions)), tuple(reversed(unknown))


def _credited_title(title: str, names: set[str]) -> str | None:
    parts = _DASH.split(title)
    if len(parts) > 1:
        if norm_artist(parts[0]) in names:
            return " - ".join(parts[1:])
        if norm_artist(parts[-1]) in names:
            return " - ".join(parts[:-1])
    return None


def _singles(
    entries: list[object], track: Track, names: set[str], album: str, *, video: bool
) -> list[Candidate]:
    expected, planned_versions, planned_unknown = _single_title(track.title, album)
    matches: list[Candidate] = []
    for raw in entries:
        data = object_value(raw)
        item = _item(data)
        if item is None or not within_duration(track.length_ms, item.length_ms):
            continue
        title = item.title
        if video:
            if track.length_ms is None or (credited := _credited_title(title, names)) is None:
                continue
            title = credited
        elif not names & _artist_names(data):
            continue
        penalty = False
        if norm_title(title) != norm_title(track.title):
            base, versions, unknown = _single_title(title, album)
            if (
                not expected
                or base != expected
                or versions != planned_versions
                or unknown[: len(planned_unknown)] != planned_unknown
            ):
                continue
            penalty = unknown != planned_unknown
        if norm_title(item.title) != norm_title(track.title):
            item = replace(item, aliases=(track.title,))
        official = not video and data.get("resultType") == "song" and bool(data.get("album"))
        candidate = _candidate(item.ref, item.title, (item,), album=False, official=official)
        matches.append(replace(candidate, title_penalty=penalty))
    return matches


class YTMusicSource:
    name = "ytmusic"

    def __init__(
        self,
        ytm: YTMusic,
        downloader: Downloader | None,
        cookies: Path | None,
    ) -> None:
        self._ytm = ytm
        self._downloader = downloader if downloader is not None else YtDlpDownloader(cookies)

    async def search(self, release: Release, tracks: list[Track]) -> list[Candidate]:
        try:
            return await asyncio.to_thread(self._search, release, tracks)
        except (YTMusicError, OSError, ValueError, RecursionError) as exc:
            reason = _diagnostic(exc, _SEARCH_URL)
            if is_wall(exc):
                raise SourceWall(reason) from None
            raise FetchError(reason) from None

    def _search(self, release: Release, tracks: list[Track]) -> list[Candidate]:
        all_tracks = release_tracks(release)
        artist_names = {norm_artist(credit.name) for credit in release.credits}
        query = f"{credit_string(release.credits)} {release.title}"
        albums = cast("list[object]", self._ytm.search(query, filter="albums"))[:5]  # pyright: ignore[reportUnknownMemberType]
        candidates: list[Candidate] = []
        for raw in albums:
            ref = object_value(raw).get("browseId")
            if not isinstance(ref, str):
                continue
            album = object_value(self._ytm.get_album(ref))  # pyright: ignore[reportUnknownMemberType]
            if not artist_names & _artist_names(album):
                continue
            items = tuple(
                item
                for entry in array_value(album.get("tracks"))
                if (item := _item(object_value(entry))) is not None
            )
            if len(items) == len(all_tracks) and len(
                match_items(all_tracks, items, release)
            ) == len(all_tracks):
                candidates.append(
                    _candidate(
                        ref,
                        str(album.get("title") or release.title),
                        items,
                        album=True,
                        official=True,
                    )
                )
        if not candidates or len(tracks) < len(all_tracks):
            album_title = norm_title(release.title)
            for track in tracks:
                credits = track.credits or release.credits
                names = {norm_artist(credit.name) for credit in credits}
                query = f"{credit_string(credits)} {track.title}"
                songs = cast("list[object]", self._ytm.search(query, filter="songs"))[:5]  # pyright: ignore[reportUnknownMemberType]
                matches = _singles(songs, track, names, album_title, video=False)
                context = (
                    f"{query} {release.title}" if norm_title(track.title) != album_title else query
                )
                if all(match.title_penalty for match in matches) and context != query:
                    songs = cast("list[object]", self._ytm.search(context, filter="songs"))[:5]  # pyright: ignore[reportUnknownMemberType]
                    matches.extend(_singles(songs, track, names, album_title, video=False))
                if all(match.title_penalty for match in matches):
                    videos = cast(
                        "list[object]",
                        self._ytm.search(context, filter="videos"),  # pyright: ignore[reportUnknownMemberType]
                    )[:5]
                    matches.extend(_singles(videos, track, names, album_title, video=True))
                matches.sort(key=lambda candidate: candidate.title_penalty)
                seen: set[str] = set()
                for candidate in matches:
                    if candidate.ref not in seen:
                        seen.add(candidate.ref)
                        candidates.append(candidate)
        return candidates

    async def fetch(self, candidate: Candidate, into: Path) -> list[Fetched]:
        return await asyncio.to_thread(self._fetch, candidate, into)

    def _fetch(self, candidate: Candidate, into: Path) -> list[Fetched]:
        files: list[Fetched] = []
        issues: dict[str, FetchIssue] = {}
        wall: SourceWall | None = None
        for index, item in enumerate(candidate.items):
            target = into / str(index)
            try:
                target.mkdir(parents=True, exist_ok=True)
                path = self._downloader.download(item.ref, target)
                info = probe(path)
            except (DownloadError, ExtractorError, PostProcessingError, FetchError, OSError) as exc:
                reason = _diagnostic(exc, _VIDEO_URL)
                if is_wall(exc):
                    wall = SourceWall(reason)
                    break
                issues[item.ref] = FetchIssue(reason)
            except AudioError as exc:
                issues[item.ref] = FetchIssue(str(exc), True)
            else:
                files.append(
                    Fetched(item.ref, path, info.codec, info.bitrate, info.codec in LOSSLESS)
                )
        if wall is not None and not files and not issues:
            raise wall
        if issues or wall:
            raise PartialFetchError(files, issues, wall)
        return files
