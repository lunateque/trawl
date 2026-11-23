import shutil
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from mutagen.flac import FLAC  # pyright: ignore[reportMissingTypeStubs]
from ytmusicapi import YTMusic  # pyright: ignore[reportMissingTypeStubs]

from trawl.config import Config, MbConfig, PlanConfig, Secrets
from trawl.db import repo
from trawl.db.connect import open_db
from trawl.db.repo import Kind, Origin, Scope
from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import Credit, Medium, Release, ReleaseGroupRef, Track
from trawl.net.client import HttpClient
from trawl.plan.run import plan_new_wants
from trawl.plan.wants import create_want


@pytest.mark.tools
async def test_plan_to_library_reuse_and_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audio_file: Path,
    jpeg: bytes,
) -> None:
    from trawl.runner.run import RunDeps, run_once
    from trawl.sources.ytmusic import YTMusicSource
    from trawl.verify.fingerprint import AcoustId

    cfg = Config(tmp_path / "library", tmp_path / "data", "a@b", MbConfig(), PlanConfig(), None)
    credit = Credit("The Band", "artist", "")
    tracks = tuple(
        Track(f"track-{i}", f"recording-{i}", i, str(i), f"Song {i}", 31_000, (credit,), ())
        for i in range(1, 4)
    )
    group = ReleaseGroupRef("group", "Album", "Album", (), "2001-01-15")
    release = Release(
        "release",
        "Album",
        "Official",
        "XW",
        "2002",
        (credit,),
        group,
        (Medium(1, "Digital Media", tracks, None, ()),),
        "",
        (),
        ("rock",),
    )
    copies = 0
    expected_recordings = [track.recording_mbid for track in tracks]

    class Downloader:
        def download(self, video_id: str, into: Path) -> Path:
            nonlocal copies
            copies += 1
            return Path(shutil.copyfile(audio_file, into / f"{video_id}.flac"))

    def search(_: YTMusic, query: str, filter: str) -> list[dict[str, object]]:
        assert filter == "albums"
        return [{"browseId": query}]

    def get_album(_: YTMusic, browse_id: str) -> dict[str, object]:
        members = tracks if browse_id.endswith("Album") else (tracks[0],)
        return {
            "artists": [{"name": credit.name}],
            "tracks": [
                {
                    "videoId": f"video-{i}",
                    "title": track.title,
                    "duration_seconds": 31,
                    "trackNumber": i,
                }
                for i, track in enumerate(members, 1)
            ],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.acoustid.org":
            assert request.method == "POST"
            assert b"meta=recordingids" in request.content
            recording = expected_recordings.pop(0)
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "results": [{"score": 0.99, "recordings": [{"id": recording}]}],
                },
            )
        if request.url.host == "coverartarchive.org":
            return httpx.Response(200, content=jpeg)
        if request.url.host == "lrclib.net":
            return httpx.Response(
                200, json={"plainLyrics": "Hello", "syncedLyrics": "[00:01.00]Hello"}
            )
        assert request.url.path in {"/ws/2/recording/other", "/ws/2/recording/expected"}
        return httpx.Response(
            200, json={"id": request.url.path.rsplit("/", 1)[-1], "title": "Recording"}
        )

    monkeypatch.setattr(YTMusic, "search", search)
    monkeypatch.setattr(YTMusic, "get_album", get_album)
    http = HttpClient("test", {}, httpx.MockTransport(handler))
    try:
        with closing(open_db(cfg.data_dir / "trawl.sqlite")) as conn:
            mb = MusicBrainzClient(http, conn, 30)
            source = YTMusicSource(YTMusic(), Downloader(), None)
            deps = RunDeps(http, mb, [source], AcoustId(http, "fixture"))
            albums: dict[str, Release] = {release.mbid: release}

            async def get_release(mbid: str) -> Release:
                return albums[mbid]

            monkeypatch.setattr(mb, "release", get_release)

            async def plan(album: Release) -> None:
                albums[album.mbid] = album
                with conn:
                    create_want(
                        conn,
                        Kind.RELEASE,
                        album.mbid,
                        Scope.ALBUM,
                        Origin.MANUAL,
                        album.title,
                        None,
                    )
                report = await plan_new_wants(conn, mb, cfg.plan)
                assert report.planned == 1 and report.tracks == album.track_count

            await plan(release)
            await run_once(conn, cfg, Secrets("fixture"), deps)
            folder = cfg.library_root / "The Band" / "2001 - Album"
            for i in range(1, 4):
                file = folder / f"{i:02} - Song {i}.flac"
                tags = FLAC(file)
                assert tags["musicbrainz_trackid"] == [f"recording-{i}"]
                assert tags["lyrics"] == ["Hello"]
                assert tags.pictures[0].data == jpeg  # pyright: ignore[reportUnknownMemberType]
                assert tags["replaygain_track_gain"] and tags["replaygain_album_gain"]
                assert file.with_suffix(".lrc").read_text(encoding="utf-8") == "[00:01.00]Hello"
            assert (folder / "cover.jpg").read_bytes() == jpeg
            assert repo.status(conn).tracks == [("done", 3)]
            assert [row[0] for row in conn.execute("SELECT outcome FROM attempts")] == [
                "accepted"
            ] * 3

            def edition(title: str, recording: str) -> Release:
                track = replace(tracks[0], mbid=f"{title}-track", recording_mbid=recording)
                return replace(
                    release,
                    mbid=title,
                    title=title,
                    media=(replace(release.media[0], tracks=(track,)),),
                )

            await plan(edition("Compilation", tracks[0].recording_mbid))
            await run_once(conn, cfg, Secrets("fixture"), deps)
            assert copies == 3
            with_reuse = conn.execute(
                "SELECT state, verified_by, path FROM tracks WHERE release_mbid='Compilation'"
            ).fetchone()
            assert (with_reuse[0], with_reuse[1]) == ("done", "reuse")
            assert FLAC(Path(with_reuse[2]))["album"] == ["Compilation"]
            assert FLAC(folder / "01 - Song 1.flac")["album"] == ["Album"]

            await plan(edition("Mismatch", "expected"))
            expected_recordings.append("other")
            await run_once(conn, cfg, Secrets("fixture"), deps)
            assert repo.status(conn).tracks == [("done", 4), ("missing", 1)]
            assert (
                conn.execute("SELECT outcome FROM attempts ORDER BY id DESC LIMIT 1").fetchone()[0]
                == "rejected_verify"
            )
            assert not list((cfg.library_root / "The Band" / "2001 - Mismatch").glob("*.flac"))
    finally:
        await http.aclose()
