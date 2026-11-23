import asyncio
import re
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import parse_qs, urlsplit

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

from trawl.mb.models import JsonObject, Release, Track, array_value, credit_string, object_value
from trawl.normalize import norm_artist, norm_title
from trawl.sources.base import (
    Candidate,
    CandidateItem,
    Fetched,
    FetchError,
    FetchIssue,
    PartialFetchError,
    SourceWall,
)
from trawl.sources.score import match_items, release_tracks, within_duration
from trawl.verify.probe import LOSSLESS, AudioError, probe

_WALL = re.compile(
    r"\bHTTP(?: Error| status(?: code)?)?[: ]+(?:403|429)\b"
    r"|\b(?:403|429) Client Error\b|sign in to confirm|not a bot",
    re.I,
)
_VIDEO = re.compile(r"[A-Za-z0-9_-]{11}")


def is_wall(exc: Exception) -> bool:
    return _WALL.search(str(exc)) is not None


def video_id(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname
    value = ""
    if parsed.scheme in {"https", "http"}:
        if host == "youtu.be":
            value = parsed.path.removeprefix("/")
        elif host in {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com"}:
            if parsed.path == "/watch":
                value = parse_qs(parsed.query).get("v", [""])[0]
            elif parsed.path.startswith("/shorts/"):
                value = parsed.path.removeprefix("/shorts/")
    if not _VIDEO.fullmatch(value):
        raise ValueError("expected a YouTube or YouTube Music video URL")
    return value


class Downloader(Protocol):
    def download(self, video_id: str, into: Path) -> Path: ...


class CopyAudio(FFmpegExtractAudioPP):
    def run_ffmpeg(self, path: str, out_path: str, codec: str, more_opts: list[str]) -> None:
        if codec != "copy":
            raise PostProcessingError("audio cannot be extracted without re-encoding")
        super().run_ffmpeg(path, out_path, codec, more_opts)  # pyright: ignore[reportUnknownMemberType]


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
        }
        if self._cookies is not None:
            options["cookiefile"] = str(self._cookies)
        with YoutubeDL(options) as ydl:  # pyright: ignore[reportUnknownVariableType]
            processor = CopyAudio(ydl, preferredcodec="best")
            ydl.add_post_processor(processor)  # pyright: ignore[reportUnknownMemberType,reportArgumentType]
            result: object = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}")  # pyright: ignore[reportUnknownMemberType]
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
        norm_artist(name)
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
        except (YTMusicError, OSError, ValueError) as exc:
            if is_wall(exc):
                raise SourceWall(str(exc)) from exc
            raise FetchError(str(exc)) from exc

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
            for track in tracks:
                credits = track.credits or release.credits
                names = {norm_artist(credit.name) for credit in credits}
                songs = cast(
                    "list[object]",
                    self._ytm.search(f"{credit_string(credits)} {track.title}", filter="songs"),  # pyright: ignore[reportUnknownMemberType]
                )[:5]
                for raw in songs:
                    song = object_value(raw)
                    item = _item(song)
                    if not (
                        item
                        and names & _artist_names(song)
                        and norm_title(item.title) == norm_title(track.title)
                        and within_duration(track.length_ms, item.length_ms)
                    ):
                        continue
                    official = song.get("resultType") == "song" and bool(song.get("album"))
                    candidates.append(
                        _candidate(item.ref, item.title, (item,), album=False, official=official)
                    )
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
            except (DownloadError, ExtractorError, FetchError, AudioError, OSError) as exc:
                if is_wall(exc):
                    wall = SourceWall(str(exc))
                    break
                issues[item.ref] = FetchIssue(str(exc), isinstance(exc, AudioError))
            else:
                files.append(
                    Fetched(item.ref, path, info.codec, info.bitrate, info.codec in LOSSLESS)
                )
        if wall is not None and not files and not issues:
            raise wall
        if issues or wall:
            raise PartialFetchError(files, issues, wall)
        return files
