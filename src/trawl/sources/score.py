from collections.abc import Mapping, Sequence
from statistics import mean

from trawl.mb.models import Release, Track
from trawl.normalize import norm_title
from trawl.sources.base import Candidate, CandidateItem

ALBUM_COVERAGE = 1.0
TRACK_COVERAGE = 0.0


def within_duration(expected: int | None, actual: int | None) -> bool:
    return expected is None or (
        actual is not None and abs(expected - actual) <= max(3000, expected * 0.02)
    )


def release_tracks(release: Release) -> list[Track]:
    return [
        track
        for medium in release.media
        for track in medium.tracks
        if track.position > 0 and track.number != "0"
    ]


def match_items(
    tracks: Sequence[Track], items: Sequence[CandidateItem], release: Release | None = None
) -> dict[str, CandidateItem]:
    wanted = {track.mbid: track for track in tracks}
    mapped: dict[str, CandidateItem] = {}
    used: set[int] = set()

    def positions(members: Sequence[Track], indices: Sequence[int], *, flattened: bool) -> None:
        if len(members) != len(indices):
            return
        by_position: dict[int | None, int] = {items[i].position: i for i in indices}
        if len(by_position) != len(indices):
            return
        for index, member in enumerate(members, 1):
            i = by_position.get(index if flattened else member.position)
            if (
                member.mbid in wanted
                and i is not None
                and i not in used
                and wanted[member.mbid].length_ms is not None
                and within_duration(wanted[member.mbid].length_ms, items[i].length_ms)
            ):
                mapped[member.mbid] = items[i]
                used.add(i)

    if release is None:
        positions(tracks, range(len(items)), flattened=False)
    elif all(item.disc is None for item in items):
        positions(release_tracks(release), range(len(items)), flattened=len(release.media) > 1)
    else:
        for medium in release.media:
            members = [t for t in medium.tracks if t.position > 0 and t.number != "0"]
            positions(
                members,
                [i for i, item in enumerate(items) if item.disc == medium.position],
                flattened=False,
            )

    titles = [norm_title(item.title) for item in items]
    pairs: list[tuple[int | float, str, int]] = []
    for track in tracks:
        if track.mbid in mapped:
            continue
        title = norm_title(track.title)
        for i, item in enumerate(items):
            if (
                i not in used
                and titles[i] == title
                and within_duration(track.length_ms, item.length_ms)
            ):
                distance = (
                    abs(track.length_ms - item.length_ms)
                    if track.length_ms is not None and item.length_ms is not None
                    else float("inf")
                )
                pairs.append((distance, track.mbid, i))
    for _, mbid, i in sorted(pairs):
        if mbid not in mapped and i not in used:
            mapped[mbid] = items[i]
            used.add(i)
    return mapped


def coverage(tracks: Sequence[Track], mapping: Mapping[str, CandidateItem]) -> float:
    return len(mapping) / len(tracks) if tracks else 0.0


def score(
    candidate: Candidate, tracks: Sequence[Track], mapping: Mapping[str, CandidateItem]
) -> float:
    errors = [
        abs(track.length_ms - item.length_ms) / 1000
        for track in tracks
        if (item := mapping.get(track.mbid)) is not None
        and track.length_ms is not None
        and item.length_ms is not None
    ]
    bitrate = candidate.bitrate or 0
    quality = 30 if candidate.lossless else 15 if bitrate >= 256 else 5 if bitrate >= 160 else 0
    reliable = (
        10
        if candidate.official and candidate.kind == "album" and candidate.source == "ytmusic"
        else 0
    )
    return (
        coverage(tracks, mapping) * 100 - 2 * (mean(errors) if errors else 0) + quality + reliable
    )
