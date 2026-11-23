from collections.abc import Callable, Sequence

from trawl.config import PlanConfig
from trawl.mb.models import Release, ReleaseGroupRef, ReleaseRef


def choose_release_group(
    groups: Sequence[ReleaseGroupRef],
    has_official: Callable[[str], bool],
) -> ReleaseGroupRef | None:
    def rank(group: ReleaseGroupRef) -> int:
        if "Compilation" in group.secondary_types:
            return 3
        if group.primary_type == "Album" and has_official(group.mbid):
            return 0
        if group.primary_type == "EP":
            return 1
        if group.primary_type == "Single":
            return 2
        return 4

    candidates = ((rank(group), group) for group in groups)
    selected = min(
        ((rank, group) for rank, group in candidates if rank < 4),
        key=lambda item: (
            item[0],
            item[1].first_release_date is None,
            item[1].first_release_date or "",
            item[1].mbid,
        ),
        default=None,
    )
    return selected[1] if selected is not None else None


def choose_recording_group(releases: Sequence[ReleaseRef]) -> ReleaseGroupRef | None:
    groups = {item.release_group.mbid: item.release_group for item in releases}
    official = {item.release_group.mbid for item in releases if item.status == "Official"}
    return choose_release_group(list(groups.values()), official.__contains__)


def medium_rank(release: Release) -> int:
    format_name = release.media[0].format if release.media else None
    return 0 if format_name == "Digital Media" else 1 if format_name == "CD" else 2


def choose_release(
    releases: Sequence[Release],
    cfg: PlanConfig,
) -> tuple[Release, dict[str, object]] | None:
    countries = tuple(dict.fromkeys(("XW", *cfg.countries)))

    def key(release: Release) -> tuple[bool, int, int, int, bool, str, str]:
        country = (
            countries.index(release.country) if release.country in countries else len(countries)
        )
        tracks = release.track_count if cfg.prefer == "original" else -release.track_count
        return (
            release.status != "Official",
            medium_rank(release),
            country,
            tracks,
            (release.date is None),
            release.date or "",
            release.mbid,
        )

    release = min(releases, key=key, default=None)
    if release is None:
        return None
    return release, {
        "status": release.status,
        "format": release.media[0].format if release.media else None,
        "country": release.country,
        "track_count": release.track_count,
        "date": release.date,
        "compared": len(releases),
        "prefer": cfg.prefer,
    }
