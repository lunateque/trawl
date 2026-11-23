import re
from os import environ
from pathlib import Path
from tomllib import TOMLDecodeError, loads
from typing import cast
from urllib.parse import urlsplit

from platformdirs import user_config_dir, user_data_dir

from trawl.config.models import (
    DEFAULT_TEMPLATE,
    Config,
    ConfigError,
    GenreConfig,
    ListenBrainzConfig,
    LocalConfig,
    MbConfig,
    PlanConfig,
    ProxyConfig,
    SoulseekConfig,
    SourcesConfig,
    TasteConfig,
    TasteTier,
    WatchConfig,
)
from trawl.config.values import (
    boolean,
    check_keys,
    integer,
    ratio,
    string,
    string_map,
    strings,
    table,
)


def config_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    if override := environ.get("TRAWL_CONFIG"):
        return Path(override)
    return Path(user_config_dir("trawl")) / "config.toml"


def _sources(values: dict[str, object], text: str) -> SourcesConfig:
    check_keys(
        values,
        {
            "order",
            "ytmusic",
            "soulseek",
            "local",
            "lossless_repack",
            "album_coverage",
            "track_coverage",
        },
        text,
        ("sources",),
    )
    ytmusic, soulseek, local = (table(values, name) for name in ("ytmusic", "soulseek", "local"))
    check_keys(ytmusic, {"concurrency"}, text, ("sources", "ytmusic"))
    check_keys(
        soulseek,
        {
            "download_dir",
            "concurrency",
            "search_timeout_s",
            "stall_timeout_s",
            "candidate_timeout_s",
        },
        text,
        ("sources", "soulseek"),
    )
    check_keys(local, {"inbox", "move"}, text, ("sources", "local"))
    order = strings(values.get("order", ("ytmusic",)), "sources.order")
    for name in order:
        if name not in {"ytmusic", "soulseek", "local"}:
            raise ConfigError(f"sources.order: unknown source {name}")
    if not order or len(order) != len(set(order)):
        raise ConfigError("sources.order: expected at least one source and no duplicates")
    download = string(soulseek, "download_dir", "").strip()
    if "soulseek" in order and not download:
        raise ConfigError("sources.soulseek.download_dir: required when soulseek is enabled")
    return SourcesConfig(
        order,
        integer(ytmusic.get("concurrency", 2), "ytmusic.concurrency", 1),
        boolean(values.get("lossless_repack", True), "sources.lossless_repack"),
        SoulseekConfig(
            Path(download).expanduser() if download else None,
            integer(soulseek.get("concurrency", 3), "soulseek.concurrency", 1),
            integer(soulseek.get("search_timeout_s", 30), "soulseek.search_timeout_s", 1),
            integer(soulseek.get("stall_timeout_s", 600), "soulseek.stall_timeout_s", 1),
            integer(soulseek.get("candidate_timeout_s", 3600), "soulseek.candidate_timeout_s", 1),
        ),
        LocalConfig(
            tuple(
                Path(item).expanduser()
                for item in strings(local.get("inbox", ()), "sources.local.inbox")
            ),
            boolean(local.get("move", False), "sources.local.move"),
        ),
        ratio(values.get("album_coverage", 1.0), "sources.album_coverage"),
        ratio(values.get("track_coverage", 0.0), "sources.track_coverage"),
    )


def _taste(values: dict[str, object], text: str) -> TasteConfig:
    sections = {
        "listenbrainz": {"user", "range", "min_track_plays"},
        "yandex": {"playlists", "likes"},
        "spotify": {"playlists", "likes"},
        "releases": {"types"},
    }
    check_keys(values, {*sections, "tiers", "follow_max_tier"}, text, ("taste",))
    for name, keys in sections.items():
        check_keys(table(values, name), keys, text, ("taste", name))
    lb = table(values, "listenbrainz")
    period = string(lb, "range", "all_time")
    if period not in {"this_week", "this_month", "this_year", "all_time"}:
        raise ConfigError("taste.listenbrainz.range: expected this_week/month/year or all_time")
    tiers = TasteConfig().tiers
    if (raw := values.get("tiers")) is not None:
        if not isinstance(raw, list) or not raw:
            raise ConfigError("taste.tiers: expected a nonempty array of tables")
        parsed: list[TasteTier] = []
        for value in cast("list[object]", raw):
            if not isinstance(value, dict):
                raise ConfigError("taste.tiers: expected a table")
            tier = cast("dict[str, object]", value)
            check_keys(tier, {"min_plays", "scope", "limit"}, text, ("taste", "tiers"))
            scope = string(tier, "scope")
            if scope != "discography" and scope != "album":
                raise ConfigError("taste.tiers.scope: expected discography or album")
            parsed.append(
                TasteTier(
                    integer(tier.get("min_plays"), "taste.tiers.min_plays", 1),
                    scope,
                    integer(tier.get("limit"), "taste.tiers.limit", 1),
                )
            )
        if len({tier.min_plays for tier in parsed}) != len(parsed):
            raise ConfigError("taste.tiers: duplicate min_plays")
        tiers = tuple(sorted(parsed, key=lambda tier: tier.min_plays, reverse=True))
    yandex, spotify = table(values, "yandex"), table(values, "spotify")
    return TasteConfig(
        ListenBrainzConfig(
            string(lb, "user", "").strip(),
            period,
            integer(lb.get("min_track_plays", 3), "taste.listenbrainz.min_track_plays", 1),
        ),
        strings(yandex.get("playlists", ()), "taste.yandex.playlists"),
        boolean(yandex.get("likes", True), "taste.yandex.likes"),
        strings(spotify.get("playlists", ()), "taste.spotify.playlists"),
        boolean(spotify.get("likes", True), "taste.spotify.likes"),
        tiers,
        integer(values.get("follow_max_tier", 2), "taste.follow_max_tier", 0),
        strings(table(values, "releases").get("types", ("album", "ep")), "taste.releases.types"),
    )


def _duration(values: dict[str, object], key: str, default: str) -> int:
    value = string(values, key, default)
    if (match := re.fullmatch(r"([1-9][0-9]{0,8})([smhd])", value)) is None:
        raise ConfigError(f"watch.{key}: expected a positive duration such as 15m")
    count, unit = match.groups()
    return int(count) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


_DNS_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")


def _proxy(values: dict[str, object], text: str) -> ProxyConfig:
    net = table(values, "net")
    check_keys(net, {"proxy"}, text, ("net",))
    proxy = table(net, "proxy")
    check_keys(proxy, {"url", "hosts"}, text, ("net", "proxy"))
    url = proxy.get("url", "")
    if not isinstance(url, str):
        raise ConfigError("net.proxy.url: expected a string")
    url = url.strip()
    if url:
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.port == 0
                or any(ch.isspace() for ch in url)
            ):
                raise ValueError
        except ValueError:
            raise ConfigError(
                "net.proxy.url: expected an HTTP(S) proxy address without credentials"
            ) from None
    hosts = tuple(
        host.rstrip(".").lower()
        for host in strings(proxy.get("hosts", ProxyConfig().hosts), "net.proxy.hosts")
    )
    if any(not _DNS_NAME.fullmatch(host.removeprefix("*.")) for host in hosts):
        raise ConfigError("net.proxy.hosts: expected host names or *.domain patterns")
    return ProxyConfig(url, hosts)


def _parse(path: Path, text: str) -> Config:
    values: dict[str, object] = loads(text)
    sections = {
        "mb": {"cache_days"},
        "plan": {"types", "countries", "prefer", "discography_limit"},
        "verify": {"acoustid_min_score"},
        "library": {"template"},
        "lyrics": {"enabled"},
        "run": {"retry_missing_days"},
        "genres": {"map", "artist"},
        "upgrade": {"older_than_days"},
        "likes": {"scope"},
        "watch": {
            "taste_every",
            "releases_every",
            "queue_every",
            "upgrade_every",
            "retry_missing_days",
        },
    }
    check_keys(
        values,
        {"library_root", "data_dir", "contact_email", "net", "sources", "taste", *sections},
        text,
    )
    for section, keys in sections.items():
        check_keys(table(values, section), keys, text, (section,))
    mb = table(values, "mb")
    plan = table(values, "plan")
    library_root = Path(string(values, "library_root", "D:/music"))
    contact = string(values, "contact_email").strip()
    if "@" not in contact:
        raise ConfigError("contact_email: expected an email address")
    defaults = PlanConfig()
    prefer = string(plan, "prefer", defaults.prefer)
    if prefer not in ("original", "deluxe"):
        raise ConfigError('plan.prefer: expected "original" or "deluxe"')
    score = ratio(
        table(values, "verify").get("acoustid_min_score", 0.8), "verify.acoustid_min_score"
    )
    lyrics = boolean(table(values, "lyrics").get("enabled", True), "lyrics.enabled")
    genres = table(values, "genres")
    likes_scope = string(table(values, "likes"), "scope", "track")
    if likes_scope != "track" and likes_scope != "album":
        raise ConfigError("likes.scope: expected track or album")
    watch = table(values, "watch")
    return Config(
        library_root=library_root,
        data_dir=Path(string(values, "data_dir", user_data_dir("trawl"))),
        contact_email=contact,
        mb=MbConfig(integer(mb.get("cache_days", MbConfig().cache_days), "mb.cache_days", 0)),
        plan=PlanConfig(
            strings(plan.get("types", defaults.types), "plan.types"),
            strings(plan.get("countries", defaults.countries), "plan.countries"),
            prefer,
            integer(
                plan.get("discography_limit", defaults.discography_limit),
                "plan.discography_limit",
                1,
            ),
        ),
        source_path=path,
        sources=_sources(table(values, "sources"), text),
        acoustid_min_score=float(score),
        template=string(table(values, "library"), "template", DEFAULT_TEMPLATE),
        lyrics_enabled=lyrics,
        retry_missing_days=integer(
            watch.get("retry_missing_days", table(values, "run").get("retry_missing_days", 7)),
            "retry_missing_days",
            1,
        ),
        genres=GenreConfig(string_map(genres, "map"), string_map(genres, "artist")),
        upgrade_older_than_days=integer(
            table(values, "upgrade").get("older_than_days", 14), "upgrade.older_than_days", 0
        ),
        taste=_taste(table(values, "taste"), text),
        likes_scope=likes_scope,
        watch=WatchConfig(
            _duration(watch, "taste_every", "6h"),
            _duration(watch, "releases_every", "24h"),
            _duration(watch, "queue_every", "15m"),
            _duration(watch, "upgrade_every", "24h"),
        ),
        proxy=_proxy(values, text),
    )


def load(path: Path) -> Config:
    try:
        return _parse(path, path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: not found; copy config.example.toml there") from exc
    except (OSError, UnicodeError, TOMLDecodeError, ConfigError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
