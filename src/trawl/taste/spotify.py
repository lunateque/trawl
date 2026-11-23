import sqlite3
from collections.abc import AsyncIterator, Mapping
from urllib.parse import urlsplit

from trawl.config import Config, ConfigError
from trawl.db.repo import Kind, Origin
from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import JsonObject, array_value, object_value
from trawl.net.client import HttpClient, HttpError
from trawl.resolve.match import ExternalTrack
from trawl.taste.store import Like, SyncReport, advance, cursor, ingest_collection, moment

BASE = "https://api.spotify.com/v1"
_ACCOUNTS_ENDPOINT = "https://accounts.spotify.com/api/token"


def text(raw: object) -> str:
    return raw if isinstance(raw, str) else ""


def names(raw: object) -> str:
    return ", ".join(text(object_value(row).get("name")) for row in array_value(raw))


def external_id(row: JsonObject, kind: str) -> str | None:
    values = row.get("external_ids")
    return (text(object_value(values).get(kind)) or None) if values is not None else None


def spotify_track(row: JsonObject, origin: Origin, ref: str) -> ExternalTrack:
    album: JsonObject = object_value(row["album"]) if row.get("album") is not None else {}
    duration = row.get("duration_ms")
    return ExternalTrack(
        origin,
        ref,
        names(row.get("artists")),
        text(row.get("name")),
        text(album.get("name")) or None,
        duration if isinstance(duration, int) and not isinstance(duration, bool) else None,
        external_id(row, "isrc"),
        external_id(album, "upc"),
    )


class Spotify:
    def __init__(self, http: HttpClient, client_id: str | None, refresh_token: str | None) -> None:
        if not client_id or not refresh_token:
            raise ConfigError(
                "Spotify needs spotify_client_id and spotify_refresh_token in secrets.toml"
            )
        self.http = http
        self.client_id = client_id
        self.refresh_token = refresh_token
        self.access_token = ""

    async def refresh(self) -> None:
        result = await spotify_token(
            self.http,
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": self.refresh_token,
            },
        )
        self.access_token = text(result.get("access_token"))
        self.refresh_token = text(result.get("refresh_token")) or self.refresh_token
        if not self.access_token:
            raise HttpError(_ACCOUNTS_ENDPOINT, 200, "missing access token in Spotify response")

    async def get(self, path: str) -> JsonObject:
        if not self.access_token:
            await self.refresh()
        for attempt in range(2):
            try:
                return object_value(
                    await self.http.api_json(
                        "GET",
                        f"{BASE}{path}",
                        headers={"Authorization": f"Bearer {self.access_token}"},
                    )
                )
            except HttpError as exc:
                if exc.status != 401 or attempt:
                    raise
                await self.refresh()
        raise AssertionError("unreachable")

    async def pages(self, path: str) -> AsyncIterator[JsonObject]:
        base = path.split("?", 1)[0]
        seen: set[str] = set()
        while path not in seen:
            seen.add(path)
            page = await self.get(path)
            for item in array_value(page.get("items")):
                yield object_value(item)
            next_url = text(page.get("next"))
            if not next_url:
                return
            parsed = urlsplit(next_url)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "api.spotify.com"
                or parsed.path != f"/v1{base}"
            ):
                raise HttpError(BASE, 200, "unexpected Spotify pagination URL")
            path = f"{base}?{parsed.query}"
        raise HttpError(BASE, 200, "Spotify pagination did not advance")

    async def likes(self, last: str | None) -> AsyncIterator[Like]:
        edge = moment(last) if last else None
        async for item in self.pages("/me/tracks?limit=50"):
            stamp = text(item.get("added_at"))
            if not stamp:
                continue
            if edge is not None and moment(stamp) < edge:
                return
            raw = item.get("track")
            if raw is not None:
                row = object_value(raw)
                if ref := text(row.get("id")):
                    yield Like(spotify_track(row, Origin.SPOTIFY, f"track:{ref}"), stamp)

    async def playlist(self, identifier: str, origin: Origin) -> AsyncIterator[ExternalTrack]:
        async for item in self.pages(f"/playlists/{identifier}/items?limit=50"):
            raw = item.get("item") or item.get("track")
            if raw is not None:
                row = object_value(raw)
                if (ref := text(row.get("id"))) and row.get("type", "track") == "track":
                    yield spotify_track(row, origin, f"playlist:{identifier}:{ref}")

    async def link(self, kind: str, identifier: str) -> list[ExternalTrack]:
        if kind == "playlist":
            return [track async for track in self.playlist(identifier, Origin.MANUAL)]
        row = await self.get(f"/{kind}s/{identifier}")
        ref = f"{kind}:{identifier}"
        if kind == "track":
            return [spotify_track(row, Origin.MANUAL, ref)]
        if kind == "album":
            return [
                ExternalTrack(
                    Origin.MANUAL,
                    ref,
                    names(row.get("artists")),
                    text(row.get("name")),
                    upc=external_id(row, "upc"),
                    kind=Kind.RELEASE_GROUP,
                )
            ]
        return [ExternalTrack(Origin.MANUAL, ref, text(row.get("name")), kind=Kind.ARTIST)]


async def spotify_token(http: HttpClient, form: Mapping[str, str]) -> JsonObject:
    return object_value(await http.post_form(_ACCOUNTS_ENDPOINT, form, follow_redirects=False))


async def sync_spotify(
    conn: sqlite3.Connection, mb: MusicBrainzClient, cfg: Config, client: Spotify
) -> SyncReport:
    key = "spotify:likes"
    likes = (
        [item async for item in client.likes(cursor(conn, key))] if cfg.taste.spotify_likes else []
    )
    tracks = [like.track for like in likes]
    for identifier in cfg.taste.spotify_playlists:
        if not identifier.isascii() or not identifier.isalnum():
            raise ConfigError("taste.spotify.playlists: expected Spotify playlist IDs")
        tracks.extend([track async for track in client.playlist(identifier, Origin.SPOTIFY)])
    report = SyncReport()
    await ingest_collection(conn, mb, cfg, Origin.SPOTIFY, tracks, report)
    advance(conn, key, likes)
    return report
