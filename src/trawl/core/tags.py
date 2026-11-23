from dataclasses import dataclass


@dataclass(frozen=True)
class TagFields:
    title: str
    artist: str
    artists: tuple[str, ...]
    albumartist: str
    album: str
    track: int
    track_total: int
    disc: int
    disc_total: int
    date: str | None
    original_date: str | None
    recording_mbid: str
    release_track_mbid: str
    release_mbid: str
    release_group_mbid: str
    artist_mbids: tuple[str, ...]
    albumartist_mbids: tuple[str, ...]
    release_types: tuple[str, ...]
    release_status: str | None
    isrcs: tuple[str, ...]
    labels: tuple[str, ...]
    genres: tuple[str, ...]
    compilation: bool
    disambiguation: str
