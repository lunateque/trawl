from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID


class MbDataError(ValueError):
    pass


class InvalidMbidError(MbDataError):
    pass


type JsonObject = Mapping[str, object]


def object_value(raw: object) -> JsonObject:
    if not isinstance(raw, dict):
        raise MbDataError("MusicBrainz response: expected an object")
    return cast("dict[str, object]", raw)


def array_value(raw: object) -> list[object]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise MbDataError("MusicBrainz response: expected an array")
    return cast("list[object]", raw)


def _optional_text(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def _text(data: JsonObject, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise MbDataError(f"MusicBrainz response: expected text for {key}")
    return value


def parse_mbid(raw: object) -> str:
    if isinstance(raw, str) and len(raw) == 36:
        try:
            mbid = str(UUID(raw))
            if raw.lower() == mbid:
                return mbid
        except ValueError:
            pass
    raise InvalidMbidError("MusicBrainz identifier: expected a UUID")


def _integer(raw: object) -> int | None:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return None


def _position(data: JsonObject) -> int:
    position = _integer(data.get("position"))
    if position is None:
        raise MbDataError("MusicBrainz response: expected a position")
    return position


def _strings(raw: object) -> tuple[str, ...]:
    return tuple(item for item in array_value(raw) if isinstance(item, str))


def _items[T](parse: Callable[[JsonObject], T], raw: object) -> tuple[T, ...]:
    return tuple(parse(object_value(item)) for item in array_value(raw))


def genre_names(raw: object) -> tuple[str, ...]:
    genres = list(map(object_value, array_value(raw)))
    genres.sort(key=lambda genre: -(_integer(genre.get("count")) or 0))
    return tuple(dict.fromkeys(_text(genre, "name") for genre in genres))


@dataclass(frozen=True)
class Credit:
    name: str
    artist_mbid: str
    joinphrase: str

    @classmethod
    def from_json(cls, data: JsonObject) -> Credit:
        artist = object_value(data.get("artist"))
        return cls(
            _optional_text(data.get("name")) or _text(artist, "name"),
            parse_mbid(artist.get("id")),
            _optional_text(data.get("joinphrase")) or "",
        )


def credit_string(credits: Iterable[Credit]) -> str:
    return "".join(credit.name + credit.joinphrase for credit in credits)


def _credits(data: JsonObject) -> tuple[Credit, ...]:
    return _items(Credit.from_json, data.get("artist-credit"))


@dataclass(frozen=True)
class ReleaseGroupRef:
    mbid: str
    title: str
    primary_type: str | None
    secondary_types: tuple[str, ...]
    first_release_date: str | None

    @classmethod
    def from_json(cls, data: JsonObject) -> ReleaseGroupRef:
        return cls(
            parse_mbid(data.get("id")),
            _text(data, "title"),
            _optional_text(data.get("primary-type")),
            _strings(data.get("secondary-types")),
            _optional_text(data.get("first-release-date")),
        )


@dataclass(frozen=True)
class Track:
    mbid: str
    recording_mbid: str
    position: int
    number: str
    title: str
    length_ms: int | None
    credits: tuple[Credit, ...]
    isrcs: tuple[str, ...]

    @classmethod
    def from_json(cls, data: JsonObject) -> Track:
        recording = object_value(data.get("recording"))
        pos = _position(data)
        return cls(
            parse_mbid(data.get("id")),
            parse_mbid(recording.get("id")),
            pos,
            _optional_text(data.get("number")) or str(pos),
            _text(data, "title"),
            _integer(data.get("length")),
            _credits(data) or _credits(recording),
            _strings(recording.get("isrcs")),
        )


@dataclass(frozen=True)
class Medium:
    position: int
    format: str | None
    tracks: tuple[Track, ...]
    pregap: Track | None
    data_tracks: tuple[Track, ...]

    @classmethod
    def from_json(cls, data: JsonObject) -> Medium:
        pregap = data.get("pregap")
        return cls(
            _position(data),
            _optional_text(data.get("format")),
            _items(Track.from_json, data.get("tracks")),
            Track.from_json(object_value(pregap)) if pregap is not None else None,
            _items(Track.from_json, data.get("data-tracks")),
        )


@dataclass(frozen=True)
class Release:
    mbid: str
    title: str
    status: str | None
    country: str | None
    date: str | None
    credits: tuple[Credit, ...]
    release_group: ReleaseGroupRef
    media: tuple[Medium, ...]
    disambiguation: str
    labels: tuple[str, ...]
    genres: tuple[str, ...]

    @property
    def track_count(self) -> int:
        return sum(
            track.position > 0 and track.number != "0"
            for medium in self.media
            for track in medium.tracks
        )

    @classmethod
    def from_json(cls, data: JsonObject) -> Release:
        labels: list[str] = []
        for item in array_value(data.get("label-info")):
            if label := object_value(item).get("label"):
                labels.append(_text(object_value(label), "name"))
        return cls(
            parse_mbid(data.get("id")),
            _text(data, "title"),
            _optional_text(data.get("status")),
            _optional_text(data.get("country")),
            _optional_text(data.get("date")),
            _credits(data),
            ReleaseGroupRef.from_json(object_value(data.get("release-group"))),
            _items(Medium.from_json, data.get("media")),
            _optional_text(data.get("disambiguation")) or "",
            tuple(labels),
            genre_names(data.get("genres")),
        )


@dataclass(frozen=True)
class ReleaseRef:
    mbid: str
    status: str | None
    country: str | None
    date: str | None
    release_group: ReleaseGroupRef

    @classmethod
    def from_json(cls, data: JsonObject) -> ReleaseRef:
        return cls(
            parse_mbid(data.get("id")),
            _optional_text(data.get("status")),
            _optional_text(data.get("country")),
            _optional_text(data.get("date")),
            ReleaseGroupRef.from_json(object_value(data.get("release-group"))),
        )


@dataclass(frozen=True)
class RecordingRef:
    mbid: str
    title: str
    length_ms: int | None
    credits: tuple[Credit, ...]
    isrcs: tuple[str, ...]
    releases: tuple[ReleaseRef, ...]

    @classmethod
    def from_json(cls, data: JsonObject) -> RecordingRef:
        return cls(
            parse_mbid(data.get("id")),
            _text(data, "title"),
            _integer(data.get("length")),
            _credits(data),
            _strings(data.get("isrcs")),
            _items(ReleaseRef.from_json, data.get("releases")),
        )


@dataclass(frozen=True)
class ArtistRef:
    mbid: str
    name: str
    sort_name: str
    score: int | None

    @classmethod
    def from_json(cls, data: JsonObject) -> ArtistRef:
        name = _text(data, "name")
        return cls(
            parse_mbid(data.get("id")),
            name,
            _optional_text(data.get("sort-name")) or name,
            _integer(data.get("score")),
        )
