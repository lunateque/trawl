import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest

from trawl.config.models import PlanConfig
from trawl.db.connect import open_db
from trawl.db.repo import Kind, Origin, Scope
from trawl.mb.client import MusicBrainzClient
from trawl.net.client import HttpClient
from trawl.plan.run import plan_new_wants
from trawl.plan.wants import (
    ForeignLink,
    MbLink,
    TextAlbum,
    TextArtist,
    create_want,
    parse_add_target,
)

OK_GROUP = "b1392450-e666-3926-a536-22c65f834433"
MULTI = "541a0976-ca45-3c0f-89e5-26bc376f58d1"
LUCKY = "79047824-f821-4b1a-9893-e0cea1c947dd"
KINO = "064db6e8-fdfb-4acb-a327-fc2de75b37de"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (f"https://musicbrainz.org/release/{MULTI}", MbLink(Kind.RELEASE, MULTI)),
        (
            f"beta.musicbrainz.org/release-group/{OK_GROUP.upper()}/?x=y",
            MbLink(Kind.RELEASE_GROUP, OK_GROUP),
        ),
        ("open.spotify.com/track/id", ForeignLink("Spotify", "https://open.spotify.com/track/id")),
        (
            "https://music.yandex.kz/album/1",
            ForeignLink("Yandex Music", "https://music.yandex.kz/album/1"),
        ),
        ("https://youtu.be/id", ForeignLink("YouTube Music", "https://youtu.be/id")),
        (
            "https://music.youtube.com/watch?v=id",
            ForeignLink("YouTube Music", "https://music.youtube.com/watch?v=id"),
        ),
        ("  Кино — Группа крови - ремастер  ", TextAlbum("Кино", "Группа крови - ремастер")),
        ("Кино", TextArtist("Кино")),
    ],
)
def test_target(target: str, expected: object) -> None:
    assert parse_add_target(target) == expected


@pytest.mark.parametrize(
    "target", ["", "Kino - ", "https://musicbrainz.org/release/bad", "https://example.com/abc"]
)
def test_invalid_target(target: str) -> None:
    with pytest.raises(ValueError):
        parse_add_target(target)


@pytest.fixture
def fixture_server(
    mb_fixture: Callable[[str], dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    editions = mb_fixture("ok_editions")["releases"]
    recording = mb_fixture("recording_lucky")
    compilation = next(
        release
        for release in recording["releases"]
        if "Compilation" in release["release-group"]["secondary-types"]
    )
    first_page = [
        {**compilation, "id": f"00000000-0000-4000-8000-{index:012d}"} for index in range(100)
    ]
    second_page = [release for release in editions if release["id"] == MULTI]

    def handler(req: httpx.Request) -> httpx.Response:
        endpoint = req.url.path.removeprefix("/ws/2/")
        if endpoint == f"release/{MULTI}":
            return httpx.Response(200, json=mb_fixture("extra_tracks_synthetic"))
        if endpoint == f"recording/{LUCKY}":
            return httpx.Response(200, json={**recording, "releases": first_page[:25]})
        if endpoint == "release-group" and req.url.params.get("artist") == KINO:
            return httpx.Response(200, json=mb_fixture("kino_groups"))
        if endpoint == "release":
            if req.url.params.get("recording") == LUCKY:
                assert req.url.params["inc"] == "release-groups"
                assert req.url.params["limit"] == "100"
                offset = req.url.params["offset"]
                assert offset in {"0", "100"}
                return httpx.Response(
                    200,
                    json={
                        "release-count": len(first_page) + len(second_page),
                        "releases": first_page if offset == "0" else second_page,
                    },
                )
            if req.url.params.get("release-group") == OK_GROUP:
                return httpx.Response(
                    200, json={"release-count": len(editions), "releases": editions}
                )
            return httpx.Response(200, json={"release-count": 0, "releases": []})
        for edition in editions:
            if endpoint == f"release/{edition['id']}":
                return httpx.Response(200, json=edition)
        return httpx.Response(404)

    return handler


@pytest.fixture
async def db_mb(tmp_path: Path, fixture_server: Callable[[httpx.Request], httpx.Response]):
    http = HttpClient("test", {}, httpx.MockTransport(fixture_server))
    try:
        with closing(open_db(tmp_path / "db.sqlite")) as db:
            yield db, MusicBrainzClient(http, db, 30)
    finally:
        await http.aclose()


def add(
    db: sqlite3.Connection, kind: Kind, mbid: str, scope: Scope, limit: int | None = None
) -> int:
    with db:
        return create_want(db, kind, mbid, scope, Origin.MANUAL, "test", limit)


async def test_plan_direct_release_and_skip_hidden_tracks(
    db_mb: tuple[sqlite3.Connection, MusicBrainzClient],
) -> None:
    db, mb = db_mb
    first = add(db, Kind.RELEASE, MULTI, Scope.ALBUM)
    assert add(db, Kind.RELEASE, MULTI, Scope.ALBUM) == first
    report = await plan_new_wants(db, mb, PlanConfig())
    assert (report.planned, report.empty, report.errors, report.tracks) == (1, 0, 0, 12)
    assert [
        tuple(row)
        for row in db.execute(
            "SELECT disc, count(*), min(disc_track_count) FROM tracks GROUP BY disc"
        )
    ] == [(1, 6, 6), (2, 6, 6)]
    assert (
        db.execute(
            "SELECT count(*) FROM tracks WHERE release_track_mbid LIKE '00000000-%'"
        ).fetchone()[0]
        == 0
    )
    assert db.execute("SELECT count(*) FROM want_tracks").fetchone()[0] == 12


async def test_plan_group_chooses_edition_and_explains(
    db_mb: tuple[sqlite3.Connection, MusicBrainzClient],
) -> None:
    db, mb = db_mb
    add(db, Kind.RELEASE_GROUP, OK_GROUP, Scope.ALBUM)
    assert (await plan_new_wants(db, mb, PlanConfig())).planned == 1
    chosen = db.execute("SELECT mbid, choice_reason FROM releases").fetchone()
    assert chosen["mbid"] == "1834eae1-741b-3c03-9ca5-0df3decb43ea"
    reason = json.loads(chosen["choice_reason"])
    assert reason["compared"] == 8
    assert reason["format"] == "CD"


async def test_recording_uses_album_on_second_page_then_links_existing_track(
    db_mb: tuple[sqlite3.Connection, MusicBrainzClient],
) -> None:
    db, mb = db_mb
    first = add(db, Kind.RECORDING, LUCKY, Scope.TRACK)
    assert (await plan_new_wants(db, mb, PlanConfig())).tracks == 1
    assert db.execute("SELECT release_group_mbid FROM releases").fetchone()[0] == OK_GROUP
    assert db.execute("SELECT mbid FROM releases").fetchone()[0] == MULTI
    assert db.execute("SELECT recording_mbid FROM tracks").fetchone()[0] == LUCKY
    duplicate = add(db, Kind.RECORDING, LUCKY, Scope.ALBUM)
    assert duplicate != first
    assert (await plan_new_wants(db, mb, PlanConfig())).tracks == 0
    assert [
        tuple(row)
        for row in db.execute("SELECT want_id, track_id FROM want_tracks ORDER BY want_id")
    ] == [(first, 1), (duplicate, 1)]


async def test_artist_children_are_planned_in_same_run(
    db_mb: tuple[sqlite3.Connection, MusicBrainzClient],
) -> None:
    db, mb = db_mb
    artist = add(db, Kind.ARTIST, KINO, Scope.DISCOGRAPHY, 3)
    report = await plan_new_wants(db, mb, PlanConfig())
    assert (report.planned, report.empty, report.errors) == (1, 3, 0)
    children = db.execute("SELECT kind, status, origin_ref FROM wants WHERE id != ?", (artist,))
    assert [tuple(child) for child in children] == [
        ("release_group", "empty", f"artist:{KINO}")
    ] * 3


async def test_request_failure_leaves_want_new_and_continues(
    db_mb: tuple[sqlite3.Connection, MusicBrainzClient],
) -> None:
    db, mb = db_mb
    broken = add(db, Kind.RELEASE, "unavailable", Scope.ALBUM)
    add(db, Kind.RELEASE, MULTI, Scope.ALBUM)
    report = await plan_new_wants(db, mb, PlanConfig())
    assert (report.planned, report.errors) == (1, 1)
    assert db.execute("SELECT status FROM wants WHERE id = ?", (broken,)).fetchone()[0] == "new"
