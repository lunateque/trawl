from dataclasses import dataclass

from trawl.config import GenreConfig
from trawl.mb.models import Release, Track, credit_string


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


def fields_for(
    release: Release,
    track: Track,
    config: GenreConfig | None = None,
    artist_genres: tuple[str, ...] = (),
) -> TagFields:
    medium = next(
        medium for medium in release.media if any(t.mbid == track.mbid for t in medium.tracks)
    )
    track_total = sum(t.position > 0 and t.number != "0" for t in medium.tracks)
    credits = track.credits or release.credits
    group = release.release_group
    types = tuple(t.lower() for t in (group.primary_type, *group.secondary_types) if t)
    raw_genres = release.genres or artist_genres
    config = config or GenreConfig()
    manual = tuple(
        dict.fromkeys(
            genre
            for credit in release.credits
            if (
                genre := config.artist.get(credit.name.casefold())
                or config.artist.get(credit.artist_mbid)
            )
        )
    )
    genres = manual or tuple(
        dict.fromkeys(config.map.get(raw.casefold(), raw) for raw in raw_genres[:3])
    )
    return TagFields(
        title=track.title,
        artist=credit_string(credits),
        artists=tuple(c.name for c in credits),
        albumartist=credit_string(release.credits),
        album=release.title,
        track=track.position,
        track_total=track_total,
        disc=medium.position,
        disc_total=len(release.media),
        date=release.date,
        original_date=group.first_release_date,
        recording_mbid=track.recording_mbid,
        release_track_mbid=track.mbid,
        release_mbid=release.mbid,
        release_group_mbid=group.mbid,
        artist_mbids=tuple(c.artist_mbid for c in credits),
        albumartist_mbids=tuple(c.artist_mbid for c in release.credits),
        release_types=types,
        release_status=release.status.lower() if release.status else None,
        isrcs=track.isrcs,
        labels=release.labels,
        genres=genres[:3],
        compilation="compilation" in types,
        disambiguation=release.disambiguation,
    )
