from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DEFAULT_TEMPLATE = (
    "{albumartist}/{original_year} - {album}{disambiguation}/"
    "{disc_prefix}{track:02} - {title}.{ext}"
)
ACOUSTID_KEY_HELP = "Set acoustid_key in secrets.toml: https://acoustid.org/new-application"


@dataclass(frozen=True)
class PlanConfig:
    types: tuple[str, ...] = ("album", "ep", "single")
    countries: tuple[str, ...] = ("XW", "US", "GB")
    prefer: Literal["original", "deluxe"] = "original"
    discography_limit: int = 14


@dataclass(frozen=True)
class MbConfig:
    cache_days: int = 30


@dataclass(frozen=True)
class SoulseekConfig:
    download_dir: Path | None = None
    concurrency: int = 3
    search_timeout_s: int = 30
    stall_timeout_s: int = 600
    candidate_timeout_s: int = 3600


@dataclass(frozen=True)
class LocalConfig:
    inbox: tuple[Path, ...] = ()
    move: bool = False


@dataclass(frozen=True)
class SourcesConfig:
    order: tuple[str, ...] = ("ytmusic",)
    ytmusic_concurrency: int = 2
    lossless_repack: bool = True
    soulseek: SoulseekConfig = SoulseekConfig()
    local: LocalConfig = LocalConfig()
    album_coverage: float = 1.0
    track_coverage: float = 0.0


@dataclass(frozen=True)
class GenreConfig:
    map: dict[str, str] = field(default_factory=dict[str, str])
    artist: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True)
class TasteTier:
    min_plays: int
    scope: Literal["discography", "album"]
    limit: int


@dataclass(frozen=True)
class ListenBrainzConfig:
    user: str = ""
    range: str = "all_time"
    min_track_plays: int = 3


@dataclass(frozen=True)
class TasteConfig:
    listenbrainz: ListenBrainzConfig = ListenBrainzConfig()
    yandex_playlists: tuple[str, ...] = ()
    yandex_likes: bool = True
    spotify_playlists: tuple[str, ...] = ()
    spotify_likes: bool = True
    tiers: tuple[TasteTier, ...] = (
        TasteTier(6, "discography", 14),
        TasteTier(3, "discography", 10),
        TasteTier(2, "discography", 3),
        TasteTier(1, "album", 1),
    )
    follow_max_tier: int = 2
    release_types: tuple[str, ...] = ("album", "ep")


@dataclass(frozen=True)
class WatchConfig:
    taste_every: int = 6 * 3600
    releases_every: int = 24 * 3600
    queue_every: int = 15 * 60
    upgrade_every: int = 24 * 3600


@dataclass(frozen=True)
class Secrets:
    acoustid_key: str | None = field(default=None, repr=False)
    youtube_cookies: Path | None = field(default=None, repr=False)
    slskd_url: str = field(default="http://localhost:5030", repr=False)
    slskd_api_key: str | None = field(default=None, repr=False)
    listenbrainz_token: str | None = field(default=None, repr=False)
    yandex_token: str | None = field(default=None, repr=False)
    spotify_client_id: str | None = field(default=None, repr=False)
    spotify_refresh_token: str | None = field(default=None, repr=False)
    slskd_allow_insecure: bool = False


@dataclass(frozen=True)
class ProxyConfig:
    url: str = ""
    hosts: tuple[str, ...] = ("archive.org", "*.archive.org", "coverartarchive.org")

    def matches(self, host: str) -> bool:
        host = host.rstrip(".").lower()
        return any(
            host.endswith(pattern[1:]) if pattern.startswith("*.") else host == pattern
            for pattern in self.hosts
        )


@dataclass(frozen=True)
class Config:
    library_root: Path
    data_dir: Path
    contact_email: str
    mb: MbConfig
    plan: PlanConfig
    source_path: Path | None
    sources: SourcesConfig = SourcesConfig()
    acoustid_min_score: float = 0.8
    template: str = DEFAULT_TEMPLATE
    lyrics_enabled: bool = True
    retry_missing_days: int = 7
    genres: GenreConfig = field(default_factory=GenreConfig)
    upgrade_older_than_days: int = 14
    taste: TasteConfig = TasteConfig()
    likes_scope: Literal["track", "album"] = "track"
    watch: WatchConfig = WatchConfig()
    proxy: ProxyConfig = ProxyConfig()


class ConfigError(Exception):
    pass
