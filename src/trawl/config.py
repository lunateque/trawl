from dataclasses import dataclass, field
from os import environ
from pathlib import Path
from tomllib import TOMLDecodeError, loads
from typing import Literal, cast
from urllib.parse import urlsplit

from platformdirs import user_config_dir, user_data_dir

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
class Secrets:
    acoustid_key: str | None = field(default=None, repr=False)
    youtube_cookies: Path | None = field(default=None, repr=False)
    slskd_url: str = field(default="http://localhost:5030", repr=False)
    slskd_api_key: str | None = field(default=None, repr=False)


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


class ConfigError(Exception):
    pass


def config_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    if override := environ.get("TRAWL_CONFIG"):
        return Path(override)
    return Path(user_config_dir("trawl")) / "config.toml"


def _key_line(text: str, keys: tuple[str, ...]) -> int:
    # Parse complete prefixes only on errors, so quoting and dotted keys follow TOML itself.
    lines = text.splitlines(keepends=True)
    start = 1
    for end in range(1, len(lines) + 1):
        try:
            value: object = loads("".join(lines[:end]))
        except TOMLDecodeError:
            continue
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                break
            value = cast("dict[str, object]", value)[key]
        else:
            return start
        start = end + 1
    return len(lines)


def _check_keys(
    values: dict[str, object], allowed: set[str], text: str, parent: tuple[str, ...] = ()
) -> None:
    for key in values:
        if key not in allowed:
            path = (*parent, key)
            raise ConfigError(f"unknown key {'.'.join(path)} (line {_key_line(text, path)})")


def _table(values: dict[str, object], key: str) -> dict[str, object]:
    raw = values.get(key, {})
    if not isinstance(raw, dict):
        raise ConfigError(f"{key}: expected a table")
    return cast("dict[str, object]", raw)


def _string(values: dict[str, object], key: str, default: str | None = None) -> str:
    raw = values.get(key, default)
    if raw is None:
        raise ConfigError(f"{key}: required")
    if not isinstance(raw, str):
        raise ConfigError(f"{key}: expected a string")
    return raw


def _strings(raw: object, key: str) -> tuple[str, ...]:
    if isinstance(raw, list | tuple):
        items: list[str] = []
        for item in cast("list[object] | tuple[object, ...]", raw):
            if not isinstance(item, str):
                break
            items.append(item)
        else:
            return tuple(items)
    raise ConfigError(f"{key}: expected an array of strings")


def _integer(raw: object, key: str, minimum: int) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ConfigError(f"{key}: expected an integer")
    if raw < minimum:
        raise ConfigError(f"{key}: expected at least {minimum}")
    return raw


def _string_map(values: dict[str, object], name: str) -> dict[str, str]:
    mapping = _table(values, name)
    return {key.casefold(): _string(mapping, key) for key in mapping}


def _boolean(raw: object, name: str) -> bool:
    if not isinstance(raw, bool):
        raise ConfigError(f"{name}: expected a boolean")
    return raw


def _ratio(raw: object, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not 0 <= raw <= 1:
        raise ConfigError(f"{name}: expected a number from 0 to 1")
    return float(raw)


def _sources(values: dict[str, object], text: str) -> SourcesConfig:
    _check_keys(
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
    ytmusic, soulseek, local = (_table(values, name) for name in ("ytmusic", "soulseek", "local"))
    _check_keys(ytmusic, {"concurrency"}, text, ("sources", "ytmusic"))
    _check_keys(
        soulseek,
        {"download_dir", "concurrency", "search_timeout_s", "stall_timeout_s"},
        text,
        ("sources", "soulseek"),
    )
    _check_keys(local, {"inbox", "move"}, text, ("sources", "local"))
    order = _strings(values.get("order", ("ytmusic",)), "sources.order")
    for name in order:
        if name not in {"ytmusic", "soulseek", "local"}:
            raise ConfigError(f"sources.order: unknown source {name}")
    if not order or len(order) != len(set(order)):
        raise ConfigError("sources.order: expected at least one source and no duplicates")
    download = _string(soulseek, "download_dir", "").strip()
    if "soulseek" in order and not download:
        raise ConfigError("sources.soulseek.download_dir: required when soulseek is enabled")
    return SourcesConfig(
        order,
        _integer(ytmusic.get("concurrency", 2), "ytmusic.concurrency", 1),
        _boolean(values.get("lossless_repack", True), "sources.lossless_repack"),
        SoulseekConfig(
            Path(download).expanduser() if download else None,
            _integer(soulseek.get("concurrency", 3), "soulseek.concurrency", 1),
            _integer(soulseek.get("search_timeout_s", 30), "soulseek.search_timeout_s", 1),
            _integer(soulseek.get("stall_timeout_s", 600), "soulseek.stall_timeout_s", 1),
        ),
        LocalConfig(
            tuple(
                Path(item).expanduser()
                for item in _strings(local.get("inbox", ()), "sources.local.inbox")
            ),
            _boolean(local.get("move", False), "sources.local.move"),
        ),
        _ratio(values.get("album_coverage", 1.0), "sources.album_coverage"),
        _ratio(values.get("track_coverage", 0.0), "sources.track_coverage"),
    )


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
    }
    _check_keys(values, {"library_root", "data_dir", "contact_email", "sources", *sections}, text)
    for section, keys in sections.items():
        _check_keys(_table(values, section), keys, text, (section,))
    mb = _table(values, "mb")
    plan = _table(values, "plan")
    library_root = Path(_string(values, "library_root", "D:/music"))
    contact = _string(values, "contact_email").strip()
    if "@" not in contact:
        raise ConfigError("contact_email: expected an email address")
    defaults = PlanConfig()
    prefer = _string(plan, "prefer", defaults.prefer)
    if prefer not in ("original", "deluxe"):
        raise ConfigError('plan.prefer: expected "original" or "deluxe"')
    score = _ratio(
        _table(values, "verify").get("acoustid_min_score", 0.8), "verify.acoustid_min_score"
    )
    lyrics = _boolean(_table(values, "lyrics").get("enabled", True), "lyrics.enabled")
    genres = _table(values, "genres")
    return Config(
        library_root=library_root,
        data_dir=Path(_string(values, "data_dir", user_data_dir("trawl"))),
        contact_email=contact,
        mb=MbConfig(_integer(mb.get("cache_days", MbConfig().cache_days), "mb.cache_days", 0)),
        plan=PlanConfig(
            _strings(plan.get("types", defaults.types), "plan.types"),
            _strings(plan.get("countries", defaults.countries), "plan.countries"),
            prefer,
            _integer(
                plan.get("discography_limit", defaults.discography_limit),
                "plan.discography_limit",
                1,
            ),
        ),
        source_path=path,
        sources=_sources(_table(values, "sources"), text),
        acoustid_min_score=float(score),
        template=_string(_table(values, "library"), "template", DEFAULT_TEMPLATE),
        lyrics_enabled=lyrics,
        retry_missing_days=_integer(
            _table(values, "run").get("retry_missing_days", 7), "run.retry_missing_days", 1
        ),
        genres=GenreConfig(_string_map(genres, "map"), _string_map(genres, "artist")),
        upgrade_older_than_days=_integer(
            _table(values, "upgrade").get("older_than_days", 14), "upgrade.older_than_days", 0
        ),
    )


def load(path: Path) -> Config:
    try:
        return _parse(path, path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: not found; copy config.example.toml there") from exc
    except (OSError, UnicodeError, TOMLDecodeError, ConfigError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def load_secrets(config: Path) -> Secrets:
    path = config.with_name("secrets.toml")
    try:
        text = path.read_text(encoding="utf-8")
        values: dict[str, object] = loads(text)
        _check_keys(values, {"acoustid_key", "youtube_cookies", "slskd_url", "slskd_api_key"}, text)
        key = _string(values, "acoustid_key", "").strip()
        cookies = _string(values, "youtube_cookies", "")
        url = _string(values, "slskd_url", "http://localhost:5030").strip()
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.port == 0
            ):
                raise ValueError
        except ValueError:
            raise ConfigError(
                "slskd_url: expected an HTTP(S) URL without credentials, query or fragment"
            ) from None
        return Secrets(
            key or None,
            path.parent / cookies if cookies else None,
            url,
            _string(values, "slskd_api_key", "").strip() or None,
        )
    except FileNotFoundError:
        return Secrets()
    except TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML") from exc
    except (OSError, UnicodeError, ConfigError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
