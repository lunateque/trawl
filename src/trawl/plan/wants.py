import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlsplit

from trawl.db.repo import Kind, Origin, Scope, insert_want


@dataclass(frozen=True)
class MbLink:
    kind: Kind
    mbid: str


@dataclass(frozen=True)
class ForeignLink:
    service: str


@dataclass(frozen=True)
class TextAlbum:
    artist: str
    title: str


@dataclass(frozen=True)
class TextArtist:
    name: str


type Target = MbLink | ForeignLink | TextAlbum | TextArtist

_MBLINK = re.compile(
    r"/(artist|release-group|release|recording)/"
    r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?",
    re.IGNORECASE | re.ASCII,
)
_MUSIC_HOSTS = re.compile(
    r"^(?:(?:beta\.)?musicbrainz\.org|open\.spotify\.com|music\.yandex\.[^/\s]+"
    r"|(?:www\.|music\.)?youtube\.com|youtu\.be)(?=[/?#]|$)",
    re.IGNORECASE,
)
_SEPARATOR = re.compile(r" (?:-|—) ")


def parse_add_target(text: str) -> Target:
    value = text.strip()
    if not value:
        raise ValueError("enter a MusicBrainz link, artist, or artist - album")
    if "://" in value or _MUSIC_HOSTS.match(value):
        url = urlsplit(value if "://" in value else f"//{value}")
        host = url.hostname or ""
        if url.scheme not in {"", "http", "https"}:
            raise ValueError("expected an HTTP or HTTPS MusicBrainz link")
        if host in {"musicbrainz.org", "beta.musicbrainz.org"}:
            if match := _MBLINK.fullmatch(url.path):
                kind, mbid = match.groups()
                return MbLink(Kind(kind.lower().replace("-", "_")), mbid.lower())
            raise ValueError(
                "invalid MusicBrainz link; expected an artist, release group, release or recording"
            )
        if host == "open.spotify.com":
            return ForeignLink("Spotify")
        if host.startswith("music.yandex."):
            return ForeignLink("Yandex Music")
        if host in {"music.youtube.com", "youtube.com", "www.youtube.com", "youtu.be"}:
            return ForeignLink("YouTube Music")
        raise ValueError("unsupported link; use a MusicBrainz link or text")
    if separator := _SEPARATOR.search(text):
        artist, title = text[: separator.start()].strip(), text[separator.end() :].strip()
        if not artist or not title:
            raise ValueError("enter both the artist and album title")
        return TextAlbum(artist, title)
    return TextArtist(value)


def create_want(
    conn: sqlite3.Connection,
    kind: Kind,
    mbid: str,
    scope: Scope,
    origin: Origin,
    origin_ref: str,
    limit: int | None = None,
) -> int:
    return insert_want(conn, kind, mbid, scope, origin, origin_ref, limit)
