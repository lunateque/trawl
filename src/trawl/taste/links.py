import asyncio
import re
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from ytmusicapi import YTMusic

from trawl.config import ConfigError, Secrets
from trawl.db.repo import Kind, Origin
from trawl.mb.models import array_value, object_value
from trawl.net.client import HttpClient
from trawl.plan.wants import ForeignLink
from trawl.resolve.match import ExternalTrack
from trawl.taste.spotify import Spotify, names, text
from trawl.taste.yandex import yandex_link

_ID = re.compile(r"[A-Za-z0-9_-]+")


def entity(link: ForeignLink) -> tuple[str, str]:
    url = urlsplit(link.url)
    parts = url.path.strip("/").split("/")
    query = parse_qs(url.query)
    if link.service == "Spotify":
        if len(parts) == 3 and parts[0].startswith("intl-"):
            parts = parts[1:]
        if (
            len(parts) == 2
            and parts[0] in {"track", "album", "artist", "playlist"}
            and parts[1].isascii()
            and parts[1].isalnum()
        ):
            return parts[0], parts[1]
    elif link.service == "Yandex Music":
        match parts:
            case ["album", album, "track", track] if (
                album.isascii() and album.isdecimal() and track.isascii() and track.isdecimal()
            ):
                return "track", f"{track}:{album}"
            case ["album" | "artist" as kind, identifier] if (
                identifier.isascii() and identifier.isdecimal()
            ):
                return kind, identifier
            case ["users", user, "playlists", identifier] if (
                _ID.fullmatch(user) and identifier.isascii() and identifier.isdecimal()
            ):
                return "playlist", f"{user}:{identifier}"
            case _:
                pass
    else:
        if url.hostname == "youtu.be" and len(parts) == 1 and _ID.fullmatch(parts[0]):
            return "track", parts[0]
        if (
            parts == ["watch"]
            and (video := query.get("v"))
            and len(video) == 1
            and _ID.fullmatch(video[0])
        ):
            return "track", video[0]
        if (
            parts == ["playlist"]
            and (ids := query.get("list"))
            and len(ids) == 1
            and _ID.fullmatch(ids[0])
        ):
            return ("album" if ids[0].startswith("OLAK5uy") else "playlist"), ids[0]
        if len(parts) == 2 and parts[0] in {"browse", "channel"} and _ID.fullmatch(parts[1]):
            return ("album" if parts[1].startswith("MPRE") else "artist"), parts[1]
    raise ConfigError(f"Invalid {link.service} link; expected track, album, artist or playlist")


def _youtube(kind: str, identifier: str) -> list[ExternalTrack]:
    try:
        yt = YTMusic()
        ref = f"{kind}:{identifier}"
        match kind:
            case "track":
                row = object_value(yt.get_song(identifier).get("videoDetails", {}))
                seconds = text(row.get("lengthSeconds"))
                return [
                    ExternalTrack(
                        Origin.MANUAL,
                        ref,
                        text(row.get("author")),
                        text(row.get("title")),
                        length_ms=int(seconds) * 1000 if seconds.isdecimal() else None,
                    )
                ]
            case "album":
                browse_id = (
                    yt.get_album_browse_id(identifier)
                    if identifier.startswith("OLAK5uy")
                    else identifier
                )
                if not browse_id:
                    return []
                row = object_value(yt.get_album(browse_id))
                return [
                    ExternalTrack(
                        Origin.MANUAL,
                        ref,
                        names(row.get("artists")),
                        text(row.get("title")),
                        kind=Kind.RELEASE_GROUP,
                    )
                ]
            case "artist":
                row = yt.get_artist(identifier)
                return [ExternalTrack(Origin.MANUAL, ref, text(row.get("name")), kind=Kind.ARTIST)]
            case "playlist":
                rows = array_value(yt.get_playlist(identifier, limit=None).get("tracks"))
                items: list[ExternalTrack] = []
                for raw in rows:
                    row = object_value(raw)
                    if video := text(row.get("videoId")):
                        album = row.get("album")
                        seconds = row.get("duration_seconds")
                        items.append(
                            ExternalTrack(
                                Origin.MANUAL,
                                f"{ref}:{video}",
                                names(row.get("artists")),
                                text(row.get("title")),
                                text(object_value(album).get("name")) if album else None,
                                seconds * 1000 if isinstance(seconds, int) else None,
                            )
                        )
                return items
            case _:
                raise ValueError("Unsupported YouTube Music link type")
    except Exception as exc:
        raise ConfigError(f"YouTube Music metadata lookup failed ({type(exc).__name__})") from None


async def metadata(http: HttpClient, secrets: Secrets, link: ForeignLink) -> list[ExternalTrack]:
    kind, identifier = entity(link)
    if link.service == "Spotify":
        client = Spotify(http, secrets.spotify_client_id, secrets.spotify_refresh_token)
        tracks = await client.link(kind, identifier)
        origin = "spotify"
    elif link.service == "Yandex Music":
        tracks = await yandex_link(secrets.yandex_token, kind, identifier)
        origin = "yandex"
    else:
        tracks = await asyncio.to_thread(_youtube, kind, identifier)
        origin = "ytmusic"
    return [replace(item, origin_ref=f"{origin}:{item.origin_ref}") for item in tracks]
