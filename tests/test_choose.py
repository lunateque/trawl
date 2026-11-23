from dataclasses import replace

import pytest

from trawl.config.models import PlanConfig
from trawl.core.catalog import Medium, Release, ReleaseGroupRef, Track
from trawl.plan.choose import choose_release, choose_release_group

GROUP = ReleaseGroupRef("a", "title", "Album", (), "2000")
TRACK = Track("t", "r", 1, "1", "track", None, (), ())
BASE = Release(
    "a",
    "title",
    "Official",
    "XW",
    "2000",
    (),
    GROUP,
    (Medium(1, "Digital Media", (TRACK,), None, ()),),
    "",
    (),
    (),
)


@pytest.mark.parametrize(
    ("alternatives", "config", "winner"),
    [
        ([replace(BASE, mbid="0", status="Promotion"), BASE], PlanConfig(), "a"),
        (
            [replace(BASE, mbid="0", media=(replace(BASE.media[0], format="CD"),)), BASE],
            PlanConfig(),
            "a",
        ),
        ([replace(BASE, mbid="0", country="GB"), BASE], PlanConfig(countries=("GB", "XW")), "a"),
        ([replace(BASE, mbid="0", country="XX"), replace(BASE, country="GB")], PlanConfig(), "a"),
        ([replace(BASE, mbid="0", date=None), BASE], PlanConfig(), "a"),
        ([replace(BASE, mbid="0", date="2001"), BASE], PlanConfig(), "a"),
        ([replace(BASE, mbid="0"), BASE], PlanConfig(), "0"),
        (
            [replace(BASE, mbid="b", media=(replace(BASE.media[0], tracks=(TRACK, TRACK)),)), BASE],
            PlanConfig(),
            "a",
        ),
        (
            [replace(BASE, mbid="b", media=(replace(BASE.media[0], tracks=(TRACK, TRACK)),)), BASE],
            PlanConfig(prefer="deluxe"),
            "b",
        ),
    ],
)
def test_release_choice(alternatives: list[Release], config: PlanConfig, winner: str) -> None:
    selection = choose_release(alternatives, config)
    assert selection is not None
    release, reason = selection
    assert release.mbid == winner
    assert reason == {
        "status": release.status,
        "format": release.media[0].format,
        "country": release.country,
        "track_count": release.track_count,
        "date": release.date,
        "compared": 2,
        "prefer": config.prefer,
    }


def test_empty_release_choice() -> None:
    assert choose_release([], PlanConfig()) is None


def test_group_order() -> None:
    groups = [
        replace(GROUP, mbid="comp", secondary_types=("Compilation",), first_release_date="1980"),
        replace(GROUP, mbid="ep", primary_type="EP", first_release_date="1990"),
        replace(GROUP, mbid="single", primary_type="Single", first_release_date="1985"),
        replace(GROUP, mbid="earlier-unofficial", first_release_date="1999"),
        replace(GROUP, mbid="b"),
        replace(GROUP, mbid="no-date", first_release_date=None),
        GROUP,
    ]
    assert choose_release_group(groups, lambda mbid: mbid != "earlier-unofficial") == GROUP
    assert choose_release_group(groups, lambda _: False) == groups[1]
    assert choose_release_group(groups[:1], lambda _: True) == groups[0]
    assert choose_release_group([], lambda _: True) is None
