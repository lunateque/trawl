from tomllib import TOMLDecodeError, loads
from typing import cast

from trawl.config.models import (
    ConfigError,
)


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


def check_keys(
    values: dict[str, object], allowed: set[str], text: str, parent: tuple[str, ...] = ()
) -> None:
    for key in values:
        if key not in allowed:
            path = (*parent, key)
            raise ConfigError(f"unknown key {'.'.join(path)} (line {_key_line(text, path)})")


def table(values: dict[str, object], key: str) -> dict[str, object]:
    raw = values.get(key, {})
    if not isinstance(raw, dict):
        raise ConfigError(f"{key}: expected a table")
    return cast("dict[str, object]", raw)


def string(values: dict[str, object], key: str, default: str | None = None) -> str:
    raw = values.get(key, default)
    if raw is None:
        raise ConfigError(f"{key}: required")
    if not isinstance(raw, str):
        raise ConfigError(f"{key}: expected a string")
    return raw


def strings(raw: object, key: str) -> tuple[str, ...]:
    if isinstance(raw, list | tuple):
        items: list[str] = []
        for item in cast("list[object] | tuple[object, ...]", raw):
            if not isinstance(item, str):
                break
            items.append(item)
        else:
            return tuple(items)
    raise ConfigError(f"{key}: expected an array of strings")


def integer(raw: object, key: str, minimum: int) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ConfigError(f"{key}: expected an integer")
    if raw < minimum:
        raise ConfigError(f"{key}: expected at least {minimum}")
    return raw


def string_map(values: dict[str, object], name: str) -> dict[str, str]:
    mapping = table(values, name)
    return {key.casefold(): string(mapping, key) for key in mapping}


def boolean(raw: object, name: str) -> bool:
    if not isinstance(raw, bool):
        raise ConfigError(f"{name}: expected a boolean")
    return raw


def ratio(raw: object, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not 0 <= raw <= 1:
        raise ConfigError(f"{name}: expected a number from 0 to 1")
    return float(raw)
