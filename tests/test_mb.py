from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from trawl.core.catalog import ArtistRef, RecordingRef, Release, ReleaseGroupRef, credit_string
from trawl.db.connect import open_db
from trawl.mb.client import MusicBrainzClient
from trawl.net.client import HttpClient


def test_release_and_tracks(mb_fixture: Callable[[str], dict[str, Any]]) -> None:
    release = Release.from_json(mb_fixture("multi_release"))
    assert release.title == "OK Computer"
    assert credit_string(release.credits) == "Radiohead"
    assert release.release_group.primary_type == "Album"
    assert release.track_count == 12
    assert [len(m.tracks) for m in release.media] == [6, 6]
    extra = Release.from_json(mb_fixture("extra_tracks_synthetic"))
    assert extra.media[0].pregap is not None
    assert extra.media[0].pregap.position == 0
    assert len(extra.media[0].data_tracks) == 1
    assert extra.track_count == 12


def test_recording_and_search_models(mb_fixture: Callable[[str], dict[str, Any]]) -> None:
    recording = RecordingRef.from_json(mb_fixture("recording_lucky"))
    assert recording.title == "Lucky"
    assert credit_string(recording.credits) == "Radiohead"
    assert {"Album", "Compilation"} <= {
        value
        for release in recording.releases
        for value in (
            release.release_group.primary_type,
            *release.release_group.secondary_types,
        )
    }
    kino = ArtistRef.from_json(mb_fixture("kino_search")["artists"][0])
    assert kino.name == "Кино"
    assert kino.score == 100
    groups = [
        ReleaseGroupRef.from_json(group) for group in mb_fixture("kino_groups")["release-groups"]
    ]
    assert any(group.primary_type == "Album" for group in groups)
    assert len(mb_fixture("ok_editions")["releases"]) > 1


async def test_cache_and_pagination(
    tmp_path: Path,
    mb_fixture: Callable[[str], dict[str, Any]],
) -> None:
    data = mb_fixture("ok_editions")["releases"]
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        params = req.url.params
        assert params["fmt"] == "json"
        assert tuple(params.keys()) == tuple(sorted(params.keys()))
        if req.url.path.endswith("/release"):
            offset = params["offset"]
            assert params["limit"] == "100"
            calls.append(offset)
            return httpx.Response(
                200, json={"release-count": 3, "releases": data[:2] if offset == "0" else data[2:3]}
            )
        calls.append("lookup")
        return httpx.Response(200, json=mb_fixture("multi_release"))

    now = datetime(2026, 1, 1, tzinfo=UTC)
    http = HttpClient("test", {}, httpx.MockTransport(handler))
    try:
        with closing(open_db(tmp_path / "db.sqlite")) as db:
            mb = MusicBrainzClient(http, db, 30, lambda: now)
            releases = await mb.release_group_releases("test")
            assert len(releases) == 3
            assert calls == ["0", "2"]
            assert releases == await mb.release_group_releases("test")
            assert calls == ["0", "2"]
            now += timedelta(days=30)
            assert releases == await mb.release_group_releases("test")
            assert calls == ["0", "2", "0", "2"]
            mb.refresh = True
            await mb.release_group_releases("test")
            assert calls == ["0", "2", "0", "2", "0", "2"]
            mb.refresh = False
            assert (await mb.release("test-release")).track_count == 12
            assert calls[-1] == "lookup"
            assert db.execute("SELECT count(*) FROM mb_cache").fetchone()[0] == 3
    finally:
        await http.aclose()
