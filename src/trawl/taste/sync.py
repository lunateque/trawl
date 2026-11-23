import logging
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from enum import StrEnum

from trawl import __version__
from trawl.config import Config, ConfigError, Secrets, load_secrets
from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import MbDataError
from trawl.net.client import HttpClient, HttpError
from trawl.taste.listenbrainz import ListenBrainz, sync_listenbrainz
from trawl.taste.releases import sync_releases
from trawl.taste.spotify import Spotify, sync_spotify
from trawl.taste.store import SyncReport
from trawl.taste.yandex import YandexError, check_yandex, sync_yandex

_LOG = logging.getLogger(__name__)
ERRORS = (ConfigError, HttpError, MbDataError, YandexError, ValueError)


class TasteSource(StrEnum):
    LISTENBRAINZ = "listenbrainz"
    YANDEX = "yandex"
    SPOTIFY = "spotify"
    RELEASES = "releases"
    ALL = "all"


def read_secrets(cfg: Config) -> Secrets:
    return load_secrets(cfg.source_path) if cfg.source_path is not None else Secrets()


@asynccontextmanager
async def taste_http(cfg: Config) -> AsyncGenerator[HttpClient]:
    http = HttpClient(f"trawl/{__version__} ( {cfg.contact_email} )", {})
    try:
        yield http
    finally:
        await http.aclose()


def enabled_sources(cfg: Config, secrets: Secrets) -> list[TasteSource]:
    enabled: list[TasteSource] = []
    if cfg.taste.listenbrainz.user:
        enabled.append(TasteSource.LISTENBRAINZ)
    if (secrets.yandex_token or cfg.taste.yandex_playlists) and (
        cfg.taste.yandex_likes or cfg.taste.yandex_playlists
    ):
        enabled.append(TasteSource.YANDEX)
    if (
        secrets.spotify_client_id or secrets.spotify_refresh_token or cfg.taste.spotify_playlists
    ) and (cfg.taste.spotify_likes or cfg.taste.spotify_playlists):
        enabled.append(TasteSource.SPOTIFY)
    return enabled


async def check_source(
    source: TasteSource, cfg: Config, secrets: Secrets, http: HttpClient
) -> None:
    match source:
        case TasteSource.LISTENBRAINZ:
            await ListenBrainz(http, cfg.taste.listenbrainz, secrets.listenbrainz_token).check()
        case TasteSource.YANDEX:
            await check_yandex(secrets.yandex_token)
        case TasteSource.SPOTIFY:
            await Spotify(http, secrets.spotify_client_id, secrets.spotify_refresh_token).refresh()
        case _:
            raise ValueError("Expected a taste source with credentials")


async def sync_taste(
    conn: sqlite3.Connection,
    mb: MusicBrainzClient,
    cfg: Config,
    selected: TasteSource,
    *,
    releases: bool = True,
) -> list[tuple[TasteSource, SyncReport, bool]]:
    secrets = read_secrets(cfg) if selected != TasteSource.RELEASES else Secrets()
    sources = enabled_sources(cfg, secrets) if selected == TasteSource.ALL else [selected]
    if selected == TasteSource.ALL and releases:
        sources.append(TasteSource.RELEASES)
    results: list[tuple[TasteSource, SyncReport, bool]] = []
    async with taste_http(cfg) as http:
        for source in sources:
            try:
                match source:
                    case TasteSource.LISTENBRAINZ:
                        if not cfg.taste.listenbrainz.user:
                            raise ConfigError("Configure taste.listenbrainz.user first")
                        client = ListenBrainz(
                            http, cfg.taste.listenbrainz, secrets.listenbrainz_token
                        )
                        report = await sync_listenbrainz(conn, mb, cfg, client)
                    case TasteSource.YANDEX:
                        report = await sync_yandex(conn, mb, cfg, secrets.yandex_token)
                    case TasteSource.SPOTIFY:
                        spotify = Spotify(
                            http, secrets.spotify_client_id, secrets.spotify_refresh_token
                        )
                        report = await sync_spotify(conn, mb, cfg, spotify)
                    case TasteSource.RELEASES:
                        report = await sync_releases(conn, mb, cfg.taste.release_types)
                    case _:
                        raise AssertionError("unexpected taste source")
                ok = source != TasteSource.RELEASES or not report.message
            except ERRORS as exc:
                _LOG.warning(
                    "taste.source_failed",
                    extra={"fields": {"source": source, "error": type(exc).__name__}},
                )
                detail = (
                    str(exc)
                    if isinstance(exc, (ConfigError, HttpError, YandexError))
                    else type(exc).__name__
                )
                report, ok = SyncReport(message=detail), False
            results.append((source, report, ok))
    return results
