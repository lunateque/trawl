import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from conftest import mbid
from yandex_music import Client, JSONType, Track, TracksList  # pyright: ignore[reportMissingTypeStubs]

from trawl.config import Config, ListenBrainzConfig, MbConfig, PlanConfig
from trawl.db.connect import open_db
from trawl.db.repo import Kind, Origin, Scope
from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import JsonObject
from trawl.net.client import HttpClient
from trawl.resolve.match import ExternalTrack, Resolved, Unresolved, resolve_album, resolve_track
from trawl.taste.listenbrainz import ListenBrainz, sync_listenbrainz, tier_for
from trawl.taste.spotify import Spotify, sync_spotify
from trawl.taste.store import SyncReport, cursor, ingest
from trawl.taste.yandex import sync_yandex


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(open_db(tmp_path / "db.sqlite")) as conn:
        yield conn


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        library_root=tmp_path / "library",
        data_dir=tmp_path,
        contact_email="test@example.org",
        mb=MbConfig(),
        plan=PlanConfig(),
        source_path=tmp_path / "config.toml",
    )


def recording(
    identifier: str, length: int | None, score: int = 100, artist: str = "Artist"
) -> JsonObject:
    return {
        "id": mbid(identifier),
        "title": "Song",
        "length": length,
        "score": score,
        "artist-credit": [{"artist": {"id": mbid("artist-id"), "name": artist}}],
    }


@pytest.mark.parametrize(
    ("isrc_hits", "text_hits", "expected"),
    [
        ([recording("isrc", 90000)], [], "isrc"),
        (
            [recording("bad", 90000), recording("nearer", 199000), recording("near", 203000)],
            [],
            "nearer",
        ),
        ([], [recording("low", 200000, 89)], None),
        ([], [recording("wrong-artist", 200000, artist="Different")], None),
        ([], [recording("wrong-length", 205000)], None),
        ([], [recording("unknown-length", None)], "unknown-length"),
        ([], [recording("near-limit", 204000)], "near-limit"),
    ],
)
async def test_recording_resolution(
    db: sqlite3.Connection,
    isrc_hits: list[JsonObject],
    text_hits: list[JsonObject],
    expected: str | None,
) -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/ws/2/recording"
        query = req.url.params["query"]
        if query.startswith("isrc:"):
            return httpx.Response(200, json={"recordings": isrc_hits})
        assert query == 'artist:"Artist" AND recording:"Song" AND release:"Album"'
        return httpx.Response(200, json={"recordings": text_hits})

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    try:
        result = await resolve_track(
            MusicBrainzClient(http, db, 30),
            ExternalTrack(Origin.YANDEX, "1", "Artist", "Song", "Album", 200000, "TESTISRC"),
        )
    finally:
        await http.aclose()
    if expected:
        assert result == Resolved(mbid(expected))
    else:
        assert isinstance(result, Unresolved)


async def test_barcode_before_album_name(db: sqlite3.Connection) -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/ws/2/release"
        assert "barcode:" in req.url.params["query"]
        return httpx.Response(
            200,
            json={
                "releases": [
                    {
                        "id": mbid("rel"),
                        "title": "Album",
                        "release-group": {
                            "id": mbid("grp"),
                            "title": "Album",
                            "primary-type": "Album",
                        },
                    }
                ]
            },
        )

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    try:
        assert await resolve_album(
            MusicBrainzClient(http, db, 30), "Wrong", "Wrong", "123"
        ) == Resolved(mbid("grp"), Kind.RELEASE_GROUP)
    finally:
        await http.aclose()


@pytest.mark.parametrize(
    ("plays", "expected"),
    [(9, (1, 14)), (6, (1, 14)), (3, (2, 10)), (2, (3, 3)), (1, (4, 1)), (0, None)],
)
def test_tier_thresholds(cfg: Config, plays: int, expected: tuple[int, int] | None) -> None:
    ranked = tier_for(plays, cfg.taste.tiers)
    assert (None if ranked is None else (ranked[0], ranked[1].limit)) == expected


async def test_listenbrainz_pages_tier_upgrade_and_pending(
    db: sqlite3.Connection, cfg: Config
) -> None:
    plays = 1
    pending = False
    pages: list[tuple[str, str]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "api.listenbrainz.org"
        assert req.headers["Authorization"] == "Token fixture"
        category = req.url.path.rsplit("/", 1)[1]
        params = req.url.params
        assert params["range"] == "this_week"
        assert params["count"] == "100"
        offset = params["offset"]
        pages.append((category, offset))
        if pending:
            return httpx.Response(204)
        if category == "artists":
            rows: list[JsonObject] = [{}] * (100 if offset == "0" else 1)
            if offset == "0":
                rows[0] = {"artist_mbid": "artist", "artist_name": "Artist", "listen_count": plays}
            return httpx.Response(
                200, json={"payload": {"artists": rows, "total_artist_count": 101}}
            )
        return httpx.Response(
            200,
            json={
                "payload": {
                    "recordings": [
                        {
                            "recording_mbid": "rec",
                            "release_group_mbid": "group",
                            "artist_name": "Artist",
                            "listen_count": 4,
                        }
                    ],
                    "total_recording_count": 1,
                }
            },
        )

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    config = ListenBrainzConfig("someone", "this_week")
    mb = MusicBrainzClient(http, db, 30)
    try:
        first = await sync_listenbrainz(db, mb, cfg, ListenBrainz(http, config, "fixture"))
        assert (first.wants, first.unresolved) == (2, 0)
        assert pages == [("artists", "0"), ("artists", "100"), ("recordings", "0")]
        assert tuple(db.execute("SELECT followed FROM artists").fetchone()) == (0,)
        assert tuple(
            db.execute('SELECT "limit",scope FROM wants WHERE kind = ?', ("artist",)).fetchone()
        ) == (1, "album")
        with db:
            db.execute("UPDATE wants SET status='planned'")
        plays = 7
        again = await sync_listenbrainz(db, mb, cfg, ListenBrainz(http, config, "fixture"))
        assert again.wants == 0
        assert tuple(
            db.execute(
                'SELECT "limit",scope,status FROM wants WHERE kind = ?', ("artist",)
            ).fetchone()
        ) == (14, "discography", "new")
        assert tuple(db.execute("SELECT followed FROM artists").fetchone()) == (1,)
        assert tuple(
            db.execute("SELECT release_group_mbid,plays FROM artist_release_plays").fetchone()
        ) == ("group", 4)
        plays = 2
        await sync_listenbrainz(db, mb, cfg, ListenBrainz(http, config, "fixture"))
        assert tuple(
            db.execute('SELECT "limit" FROM wants WHERE kind = ?', ("artist",)).fetchone()
        ) == (14,)
        pending = True
        result = await sync_listenbrainz(db, mb, cfg, ListenBrainz(http, config, "fixture"))
        assert "not computed" in result.message
        assert db.execute("SELECT count(*) FROM artist_release_plays").fetchone()[0] == 1
    finally:
        await http.aclose()


def spotify_row(identifier: str) -> JsonObject:
    return {
        "id": identifier,
        "type": "track",
        "name": "Song",
        "artists": [{"name": "Artist"}],
        "duration_ms": 200000,
        "external_ids": {"isrc": identifier},
        "album": {"name": "Album", "external_ids": {"upc": "123"}},
    }


async def test_spotify_refresh_pages_and_cursor(db: sqlite3.Connection, cfg: Config) -> None:
    phase = token_requests = 0
    searches: list[str] = []

    def like(identifier: str, day: int) -> JsonObject:
        return {"added_at": f"2026-01-{day:02}T00:00:00Z", "track": spotify_row(identifier)}

    def handle(req: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        path = req.url.path
        if path == "/api/token":
            token_requests += 1
            assert req.method == "POST"
            return httpx.Response(200, json={"access_token": f"token-{token_requests}"})
        if req.url.host == "musicbrainz.org":
            identifier = req.url.params["query"].split(":", 1)[1].strip('"')
            searches.append(identifier)
            return httpx.Response(200, json={"recordings": [recording(identifier, 200000)]})
        if req.headers["Authorization"] == "Bearer token-1":
            return httpx.Response(401)
        if path == "/v1/playlists/playlist/items":
            return httpx.Response(
                200, json={"items": [{"item": spotify_row("listtrack")}], "next": None}
            )
        assert path == "/v1/me/tracks"
        if phase:
            return httpx.Response(
                200,
                json={"items": [like("newest", 9), like("new", 8), like("old", 7)], "next": None},
            )
        if req.url.params.get("offset"):
            return httpx.Response(200, json={"items": [like("old", 7)], "next": None})
        return httpx.Response(
            200,
            json={
                "items": [like("new", 8)],
                "next": "https://api.spotify.com/v1/me/tracks?limit=50&offset=50",
            },
        )

    cfg = replace(cfg, taste=replace(cfg.taste, spotify_playlists=("playlist",)))
    http = HttpClient("test", {}, httpx.MockTransport(handle))
    try:
        mb = MusicBrainzClient(http, db, 30)
        spotify = Spotify(http, "fixture-client", "fixture-refresh")
        assert (await sync_spotify(db, mb, cfg, spotify)).wants == 3
        assert token_requests == 2
        assert searches == ["new", "old", "listtrack"]
        assert cursor(db, "spotify:likes") == "2026-01-08T00:00:00+00:00"
        phase = 1
        assert (await sync_spotify(db, mb, cfg, spotify)).wants == 1
        assert searches[-1] == "newest"
        assert len(searches) == 4
        assert cursor(db, "spotify:likes") == "2026-01-09T00:00:00+00:00"
    finally:
        await http.aclose()


async def test_yandex_uses_sdk_objects_and_only_fetches_new_likes(
    db: sqlite3.Connection,
    cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client()
    phase = 0
    requested: list[list[str]] = []

    def likes() -> TracksList:
        tracks: list[dict[str, JSONType]] = [
            {"id": "2", "timestamp": "2026-01-02T12:00:00Z"},
            {"id": "1", "timestamp": "2026-01-01T12:00:00Z"},
        ]
        if phase:
            tracks.insert(0, {"id": "3", "timestamp": "2026-01-03T12:00:00Z"})
        value = TracksList.de_json({"uid": 1, "revision": 1, "tracks": tracks}, client)
        assert value is not None
        return value

    def fetch(ids: list[str]) -> list[Track]:
        requested.append(ids)
        return list(
            Track.de_list(
                [
                    {
                        "id": identifier,
                        "title": "Song",
                        "artists": [{"id": 42, "name": "Artist"}],
                        "albums": [{"id": 87, "title": "Album"}],
                        "duration_ms": 200000,
                        "isrc": identifier,
                    }
                    for identifier in ids
                ],
                client,
            )
        )

    def make_client(_token: str | None) -> Client:
        return client

    monkeypatch.setattr("trawl.taste.yandex._client", make_client)
    monkeypatch.setattr(client, "users_likes_tracks", likes)
    monkeypatch.setattr(client, "tracks", fetch)

    def handle(req: httpx.Request) -> httpx.Response:
        identifier = req.url.params["query"].split(":", 1)[1].strip('"')
        return httpx.Response(200, json={"recordings": [recording(identifier, 200000)]})

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    try:
        mb = MusicBrainzClient(http, db, 30)
        assert (await sync_yandex(db, mb, cfg, "fixture")).wants == 2
        phase = 1
        assert (await sync_yandex(db, mb, cfg, "fixture")).wants == 1
        assert requested == [["2", "1"], ["3"]]
        assert cursor(db, "yandex:likes") == "2026-01-03T12:00:00+00:00"
    finally:
        await http.aclose()


async def test_album_scope_uses_canonical_group_only_for_likes(
    db: sqlite3.Connection, cfg: Config
) -> None:
    def release(identifier: str, kind: str, year: str) -> JsonObject:
        return {
            "id": mbid(f"release-{identifier}"),
            "title": identifier,
            "status": "Official",
            "release-group": {
                "id": mbid(identifier),
                "title": identifier,
                "primary-type": kind,
                "first-release-date": year,
            },
        }

    def handle(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/ws/2/release"
        assert req.url.params["recording"] == "recording"
        return httpx.Response(
            200,
            json={
                "release-count": 3,
                "releases": [
                    release("single", "Single", "2010"),
                    release("later", "Album", "2018"),
                    release("canonical", "Album", "2011"),
                ],
            },
        )

    http = HttpClient("test", {}, httpx.MockTransport(handle))
    try:
        mb = MusicBrainzClient(http, db, 30)
        cfg = replace(cfg, likes_scope="album")
        report = SyncReport()
        for origin, ref in (
            (Origin.SPOTIFY, "track:like"),
            (Origin.SPOTIFY, "playlist:p:like"),
            (Origin.LISTENBRAINZ, "recording:recording"),
        ):
            await ingest(
                db, mb, cfg, ExternalTrack(origin, ref, "Artist"), report, known_mbid="recording"
            )
        assert report.wants == 3
        assert [
            tuple(row) for row in db.execute("SELECT kind,scope,mbid FROM wants ORDER BY id")
        ] == [
            (Kind.RELEASE_GROUP, Scope.ALBUM, mbid("canonical")),
            (Kind.RECORDING, Scope.TRACK, "recording"),
            (Kind.RECORDING, Scope.TRACK, "recording"),
        ]
    finally:
        await http.aclose()
