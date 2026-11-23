from dataclasses import dataclass

from trawl.db.repo import Kind, Origin
from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import ArtistRef
from trawl.normalize import norm_artist, norm_title
from trawl.sources.score import within_duration


@dataclass(frozen=True)
class ExternalTrack:
    origin: Origin
    origin_ref: str
    artist: str = ""
    title: str = ""
    album: str | None = None
    length_ms: int | None = None
    isrc: str | None = None
    upc: str | None = None
    kind: Kind = Kind.RECORDING


@dataclass(frozen=True)
class Resolved:
    mbid: str
    kind: Kind = Kind.RECORDING


@dataclass(frozen=True)
class Unresolved:
    reason: str


async def resolve_track(mb: MusicBrainzClient, track: ExternalTrack) -> Resolved | Unresolved:
    if track.isrc:
        hits = await mb.search_recordings("", "", isrc=track.isrc)
        if len(hits) == 1:
            return Resolved(hits[0][0].mbid)
        if track.length_ms is not None:
            matches = [
                (abs(item.length_ms - track.length_ms), item.mbid)
                for item, _ in hits
                if item.length_ms is not None and within_duration(track.length_ms, item.length_ms)
            ]
            if matches:
                return Resolved(min(matches)[1])
    if track.artist and track.title:
        artist = norm_artist(track.artist)
        for item, score in await mb.search_recordings(track.artist, track.title, track.album):
            if (
                score >= 90
                and any(norm_artist(credit.name) == artist for credit in item.credits)
                and (
                    item.length_ms is None
                    or track.length_ms is None
                    or within_duration(track.length_ms, item.length_ms)
                )
            ):
                return Resolved(item.mbid)
    return Unresolved("No recording matched ISRC or artist, title and duration")


async def resolve_album(
    mb: MusicBrainzClient, artist: str, title: str, upc: str | None
) -> Resolved | Unresolved:
    if upc:
        hits = await mb.search_barcode(upc)
        if hits:
            return Resolved(hits[0].release_group.mbid, Kind.RELEASE_GROUP)
    if artist and title:
        for group, score, credit in await mb.search_release_groups(artist, title):
            if (
                score >= 90
                and norm_artist(artist) == norm_artist(credit)
                and (norm_title(title) == norm_title(group.title))
            ):
                return Resolved(group.mbid, Kind.RELEASE_GROUP)
    return Unresolved("No release group matched barcode or artist and album")


async def resolve_artist(mb: MusicBrainzClient, artist: str) -> ArtistRef | None:
    return next(
        (
            hit
            for hit in await mb.search_artists(artist)
            if (hit.score or 0) >= 90 and norm_artist(hit.name) == norm_artist(artist)
        ),
        None,
    )


async def resolve(mb: MusicBrainzClient, external: ExternalTrack) -> Resolved | Unresolved:
    if external.kind == Kind.ARTIST:
        if external.artist and (hit := await resolve_artist(mb, external.artist)):
            return Resolved(hit.mbid, Kind.ARTIST)
        return Unresolved("No exact artist with MusicBrainz score >= 90")
    if external.kind == Kind.RELEASE_GROUP:
        return await resolve_album(
            mb, external.artist, external.album or external.title, external.upc
        )
    return await resolve_track(mb, external)
