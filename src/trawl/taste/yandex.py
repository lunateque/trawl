import asyncio
import sqlite3
from collections.abc import Sequence
from dataclasses import replace

from yandex_music import Client, Playlist, Track  # pyright: ignore[reportMissingTypeStubs]
from yandex_music.exceptions import YandexMusicError  # pyright: ignore[reportMissingTypeStubs]

from trawl.config.models import Config, ConfigError
from trawl.db.repo import Kind, Origin
from trawl.mb.client import MusicBrainzClient
from trawl.resolve.match import ExternalTrack
from trawl.taste.store import Like, SyncReport, advance, cursor, ingest_collection, moment


class YandexError(ValueError):
    pass


def _names(artists: Sequence[object]) -> str:
    return ", ".join(
        name for artist in artists if isinstance(name := getattr(artist, "name", None), str)
    )


def yandex_track(row: Track, origin: Origin, ref: str) -> ExternalTrack:
    return ExternalTrack(
        origin,
        ref,
        _names(row.artists),
        row.title or "",
        row.albums[0].title if row.albums else None,
        row.duration_ms,
        row.isrc,
    )


def _list(playlist: Playlist, origin: Origin) -> list[ExternalTrack]:
    return [
        yandex_track(track, origin, f"playlist:{playlist.uid}:{playlist.kind}:{track.id}")
        for short in playlist.tracks or ()
        if (track := short.track or short.fetch_track())
    ]


def _client(token: str | None) -> Client:
    if not token:
        raise ConfigError("Yandex Music needs yandex_token in secrets.toml")
    return Client(token).init()


def _fetch(
    token: str | None, cfg: Config, last: str | None, known: set[str]
) -> tuple[list[Like], list[ExternalTrack]]:
    try:
        client = _client(token)
        likes: list[Like] = []
        if cfg.taste.yandex_likes and (result := client.users_likes_tracks()) is not None:
            edge = moment(last) if last else None
            new = [
                short
                for short in result.tracks
                if short.timestamp
                and (
                    edge is None
                    or moment(short.timestamp) > edge
                    or (moment(short.timestamp) == edge and f"track:{short.id}" not in known)
                )
            ]
            if new:
                times = {str(short.id): short.timestamp for short in new}
                for track in replace(result, tracks=new).fetch_tracks():
                    if timestamp := times.get(str(track.id)):
                        likes.append(
                            Like(yandex_track(track, Origin.YANDEX, f"track:{track.id}"), timestamp)
                        )
        tracks: list[ExternalTrack] = []
        if cfg.taste.yandex_playlists:
            selected = set(cfg.taste.yandex_playlists)
            for row in client.users_playlists_list():
                if row.kind is not None and (str(row.kind) in selected or row.title in selected):
                    playlist = client.users_playlists(row.kind)
                    if isinstance(playlist, Playlist):
                        tracks.extend(_list(playlist, Origin.YANDEX))
        return likes, tracks
    except YandexMusicError as exc:
        raise YandexError(f"Yandex Music request failed ({type(exc).__name__})") from None


async def check_yandex(token: str | None) -> None:
    def check() -> None:
        try:
            if _client(token).account_status() is None:
                raise YandexError("Yandex Music account not found")
        except YandexMusicError as exc:
            raise YandexError(f"Yandex Music request failed ({type(exc).__name__})") from None

    await asyncio.to_thread(check)


async def sync_yandex(
    conn: sqlite3.Connection,
    mb: MusicBrainzClient,
    cfg: Config,
    token: str | None,
) -> SyncReport:
    key = "yandex:likes"
    last = cursor(conn, key)
    known: set[str] = {
        ref
        for (ref,) in conn.execute(
            """SELECT origin_ref FROM resolved_refs WHERE origin='yandex'
            UNION SELECT origin_ref FROM unresolved WHERE origin='yandex'"""
        )
    }
    likes, tracks = await asyncio.to_thread(_fetch, token, cfg, last, known)
    report = SyncReport()
    await ingest_collection(
        conn, mb, cfg, Origin.YANDEX, [*(like.track for like in likes), *tracks], report
    )
    advance(conn, key, likes)
    return report


def _link(token: str | None, kind: str, identifier: str) -> list[ExternalTrack]:
    try:
        client = _client(token)
        ref = f"{kind}:{identifier}"
        if kind == "track":
            return [yandex_track(row, Origin.MANUAL, ref) for row in client.tracks(identifier)[:1]]
        if kind == "album":
            album = client.albums_with_tracks(identifier)
            if album:
                return [
                    ExternalTrack(
                        Origin.MANUAL,
                        ref,
                        _names(album.artists),
                        album.title or "",
                        kind=Kind.RELEASE_GROUP,
                    )
                ]
        if kind == "artist":
            return [
                ExternalTrack(Origin.MANUAL, ref, artist.name or "", kind=Kind.ARTIST)
                for artist in client.artists(identifier)[:1]
            ]
        if kind == "playlist":
            user, _, playlist_id = identifier.partition(":")
            playlist = client.users_playlists(playlist_id, user_id=user)
            if isinstance(playlist, Playlist):
                return _list(playlist, Origin.MANUAL)
        return []
    except YandexMusicError as exc:
        raise YandexError(f"Yandex Music request failed ({type(exc).__name__})") from None


async def yandex_link(token: str | None, kind: str, identifier: str) -> list[ExternalTrack]:
    return await asyncio.to_thread(_link, token, kind, identifier)
