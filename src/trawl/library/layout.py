import re
from collections.abc import Callable
from pathlib import PurePath
from string import Formatter

from trawl.core.tags import TagFields

_COMPONENT_LIMIT = 120
_PATH_LIMIT = 240
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = re.compile(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³]", re.I)


def safe_name(value: str) -> str:
    value = _FORBIDDEN.sub("_", value).rstrip(" .") or "_"
    stem, dot, rest = value.partition(".")
    if _RESERVED.fullmatch(stem.rstrip(" ")):
        value = f"{stem.rstrip(' ')}_{dot}{rest}"
    return value


def _fit(value: str, size: int, keep: str = "") -> str:
    if len(value) <= size:
        return value
    stem = value.removesuffix(keep) if keep else value
    return safe_name(stem[: size - len(keep)].rstrip(" .") + keep)


def render(template: str, fields: TagFields, ext: str, disc_count: int) -> PurePath:
    values: dict[str, str | int] = {
        "albumartist": safe_name(fields.albumartist),
        "album": safe_name(fields.album),
        "title": safe_name(fields.title),
        "track": fields.track,
        "disc": fields.disc,
        "original_year": (fields.original_date or fields.date or "")[:4] or "Unknown",
        "disambiguation": f" ({safe_name(fields.disambiguation)})" if fields.disambiguation else "",
        "disc_prefix": f"{fields.disc}-" if disc_count > 1 else "",
        "ext": safe_name(ext.lstrip(".")),
    }
    parts = template.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("library.template must be a relative path with non-empty components")
    formatter = Formatter()
    for part in parts:
        for _, field, spec, conversion in formatter.parse(part):
            if field is not None and (
                field not in values
                or conversion
                or (spec and (field not in {"track", "disc"} or spec != "02"))
            ):
                raise ValueError(
                    f"library.template: unsupported field {field!r} or format {spec!r}"
                )
    suffix = f".{values['ext']}"

    def draw() -> list[str]:
        return [safe_name(part.format_map(values)) for part in parts]

    names = draw()
    for key in ("title", "album"):
        value = str(values[key])
        while len(value) > 1 and (
            max(map(len, names)) > _COMPONENT_LIMIT or len("/".join(names)) > _PATH_LIMIT
        ):
            value = value[:-1].rstrip(" .") or "_"
            values[key] = value
            names = draw()
    names = [
        _fit(
            name, _COMPONENT_LIMIT, suffix if i == len(names) - 1 and name.endswith(suffix) else ""
        )
        for i, name in enumerate(names)
    ]
    if len("/".join(names)) > _PATH_LIMIT:
        raise ValueError("library.template cannot fit within 240 characters")
    return PurePath(*names)


def resolve_collision(
    path: PurePath,
    release_mbid: str,
    existing: Callable[[PurePath], str | None],
) -> PurePath:
    if existing(path) in {None, release_mbid}:
        return path
    suffix = f" [{release_mbid[:8]}]"
    if path.parent == PurePath():
        base = f"{path.stem}{suffix}{path.suffix}"
        return path.with_name(_fit(base, _COMPONENT_LIMIT, suffix + path.suffix))
    parent = path.parent
    album = _fit(parent.name + suffix, _COMPONENT_LIMIT, suffix)
    resolved = parent.with_name(album) / path.name
    if (excess := len(resolved.as_posix()) - _PATH_LIMIT) > 0:
        resolved = resolved.with_name(_fit(path.name, len(path.name) - excess, path.suffix))
    return resolved
