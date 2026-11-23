from dataclasses import dataclass, field
from os import environ
from pathlib import Path
from tomllib import TOMLDecodeError, loads
from typing import Literal, cast

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
class SourcesConfig:
    order: tuple[str, ...] = ("ytmusic",)
    ytmusic_concurrency: int = 2


@dataclass(frozen=True)
class GenreConfig:
    map: dict[str, str] = field(default_factory=dict[str, str])
    artist: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True)
class Secrets:
    acoustid_key: str | None = field(default=None, repr=False)
    youtube_cookies: Path | None = field(default=None, repr=False)


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


def _sources(values: dict[str, object], text: str) -> SourcesConfig:
    _check_keys(values, {"order", "ytmusic"}, text, ("sources",))
    ytmusic = _table(values, "ytmusic")
    _check_keys(ytmusic, {"concurrency"}, text, ("sources", "ytmusic"))
    order = _strings(values.get("order", ("ytmusic",)), "sources.order")
    for name in order:
        if name in {"soulseek", "local"}:
            step = 3 if name == "soulseek" else 4
            raise ConfigError(f"sources.order: {name} is available from step {step}")
        if name != "ytmusic":
            raise ConfigError(f"sources.order: unknown source {name}")
    if order != ("ytmusic",):
        raise ConfigError("sources.order: expected ytmusic once")
    return SourcesConfig(order, _integer(ytmusic.get("concurrency", 2), "ytmusic.concurrency", 1))


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
    score = _table(values, "verify").get("acoustid_min_score", 0.8)
    if isinstance(score, bool) or not isinstance(score, int | float) or not 0 <= score <= 1:
        raise ConfigError("verify.acoustid_min_score: expected a number from 0 to 1")
    lyrics = _table(values, "lyrics").get("enabled", True)
    if not isinstance(lyrics, bool):
        raise ConfigError("lyrics.enabled: expected a boolean")
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
        _check_keys(values, {"acoustid_key", "youtube_cookies"}, text)
        key = _string(values, "acoustid_key", "").strip()
        cookies = _string(values, "youtube_cookies", "")
        return Secrets(key or None, path.parent / cookies if cookies else None)
    except FileNotFoundError:
        return Secrets()
    except TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML") from exc
    except (OSError, UnicodeError, ConfigError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
