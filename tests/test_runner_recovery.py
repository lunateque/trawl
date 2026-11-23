import shutil
import sqlite3
from asyncio import to_thread
from collections.abc import Callable, Sequence
from contextlib import aclosing, closing
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from conftest import mbid
from test_folder_sources import album, tone

from trawl.config.models import Config, MbConfig, PlanConfig, Secrets
from trawl.core.catalog import Release, Track, release_tracks
from trawl.core.fetch import (
    Candidate,
    CandidateItem,
    Fetched,
    FetchIssue,
    PartialFetchError,
    SourceWall,
)
from trawl.core.tags import TagFields
from trawl.db import queue, repo, review
from trawl.db.connect import open_db
from trawl.db.repo import Kind, Origin, Scope
from trawl.mb.client import MusicBrainzClient
from trawl.net.client import HttpClient
from trawl.plan.wants import create_want
from trawl.runner.context import RunDeps
from trawl.runner.run import run_once
from trawl.runner.tools import default_audio_tools
from trawl.sources.local import LocalSource
from trawl.verify.fingerprint import AcoustId
from trawl.verify.probe import AudioError


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        tmp_path / "library",
        tmp_path / "data",
        "a@b",
        MbConfig(),
        PlanConfig(),
        None,
        lyrics_enabled=False,
    )


def _edition(*, single: bool = False) -> Release:
    release = album()
    return replace(
        release,
        mbid=mbid("runner-original"),
        release_group=replace(release.release_group, mbid=mbid("runner-group")),
        media=release.media[:1] if single else release.media,
        genres=("rock",),
    )


def _seed(conn: sqlite3.Connection, release: Release) -> list[int]:
    with conn:
        repo.upsert_release(conn, release, {})
        return [
            repo.insert_track(conn, release, medium, len(medium.tracks), track)[0]
            for medium in release.media
            for track in medium.tracks
        ]


def _test_http(recording: str | None = None) -> HttpClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.acoustid.org":
            results = [{"score": 0.99, "recordings": [{"id": recording}]}] if recording else []
            return httpx.Response(200, json={"status": "ok", "results": results})
        assert request.url.host == "coverartarchive.org", request.url
        return httpx.Response(404)

    return HttpClient("test", {}, httpx.MockTransport(handle))


def _mb(
    http: HttpClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *releases: Release,
) -> MusicBrainzClient:
    async def lookup(identifier: str) -> Release:
        return index[identifier]

    async def lookup_editions(_group: str) -> list[Release]:
        return []

    index = {release.mbid: release for release in releases}
    client = MusicBrainzClient(http, conn, 30)
    monkeypatch.setattr(client, "release", lookup)
    monkeypatch.setattr(client, "release_group_releases", lookup_editions)
    return client


@pytest.mark.tools
async def test_same_run_places_the_replacement_edition(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preferred = _edition()
    replacement = replace(
        preferred,
        mbid=mbid("runner-replacement"),
        media=tuple(
            replace(
                medium,
                tracks=(replace(medium.tracks[0], mbid=mbid(f"replacement-{medium.position}")),),
            )
            for medium in preferred.media
        ),
    )
    preferred = replace(
        preferred,
        media=tuple(
            replace(
                medium, tracks=(replace(medium.tracks[0], title="Different", length_ms=60_000),)
            )
            for medium in preferred.media
        ),
    )
    inbox = tmp_path / "inbox"
    folder = inbox / "The Band - The Album"
    tone(folder / "CD1" / "01 First Song.flac", "flac")
    tone(folder / "CD2" / "01 Second Song.flac", "flac")
    browsed: list[str] = []

    async def editions(group: str) -> list[Release]:
        browsed.append(group)
        return [preferred, replacement]

    async with aclosing(_test_http()) as http:
        with closing(open_db(cfg.data_dir / "trawl.sqlite")) as conn:
            with conn:
                want = create_want(
                    conn,
                    Kind.RELEASE_GROUP,
                    preferred.release_group.mbid,
                    Scope.ALBUM,
                    Origin.MANUAL,
                    "fixture",
                )
                for track_id in _seed(conn, preferred):
                    repo.link_track(conn, want, track_id)
            client = _mb(http, conn, monkeypatch, preferred, replacement)
            monkeypatch.setattr(client, "release_group_releases", editions)
            deps = RunDeps(http, client, [LocalSource((inbox,))], AcoustId(http, "test"))
            report = await run_once(conn, cfg, Secrets(acoustid_key="test"), deps)
            assert report.states == [("done", 2)]
            rows = conn.execute(
                """SELECT release_mbid, disc, path FROM tracks
                JOIN want_tracks ON track_id=id WHERE want_id=? ORDER BY disc""",
                (want,),
            )
            placements = [(release_mbid, disc, Path(path)) for release_mbid, disc, path in rows]
            assert [(release_mbid, disc) for release_mbid, disc, _ in placements] == [
                (replacement.mbid, 1),
                (replacement.mbid, 2),
            ]
            assert await to_thread(lambda: all(path.is_file() for _, _, path in placements))
    assert browsed == [preferred.release_group.mbid]


class _Source:
    name = "ytmusic"

    def __init__(self, audio: Path, *, on_fetch: Callable[[], None] | None = None) -> None:
        self.audio = audio
        self.on_fetch = on_fetch
        self.fetches = 0

    async def search(self, release: Release, tracks: list[Track]) -> list[Candidate]:
        items = tuple(
            CandidateItem(track.mbid, track.position, track.length_ms, track.title, disc)
            for disc, track in enumerate(tracks, 1)
        )
        return [
            Candidate(self.name, "album", "album", release.title, "flac", None, True, True, items)
        ]

    async def fetch(self, candidate: Candidate, into: Path) -> list[Fetched]:
        self.fetches += 1
        if self.on_fetch is not None:
            self.on_fetch()
        first, *rest = candidate.items
        staged = into / "audio.flac"
        shutil.copyfile(self.audio, staged)
        files = [Fetched(first.ref, staged, "flac", None, True)]
        if rest:
            raise PartialFetchError(
                files,
                {rest[0].ref: FetchIssue("decoder rejected file", probe_failed=True)},
                SourceWall("login required"),
            )
        return files


@pytest.mark.tools
async def test_source_wall_preserves_partial_success_and_defers_only_the_remaining_track(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _edition()
    audio = tmp_path / "source.flac"
    tone(audio, "flac")
    source = _Source(audio)
    async with aclosing(_test_http()) as http:
        with closing(open_db(cfg.data_dir / "trawl.sqlite")) as conn:
            first, second = _seed(conn, release)
            deps = RunDeps(
                http, _mb(http, conn, monkeypatch, release), [source], AcoustId(http, "test")
            )
            report = await run_once(conn, cfg, Secrets(acoustid_key="test"), deps)
            assert report.states == [("done", 1), ("planned", 1)]
            assert [pause.source for pause in report.pauses] == [source.name]
            rows = conn.execute(
                "SELECT state, path, next_try_at FROM tracks ORDER BY id"
            ).fetchall()
            placed = Path(rows[0][1])
            assert rows[0][2] is None and await to_thread(placed.is_file)
            assert tuple(rows[1]) == ("planned", None, queue.pause_until(conn, source.name))
            assert [
                tuple(row)
                for row in conn.execute("SELECT track_id, outcome FROM attempts ORDER BY id")
            ] == [(second, "rejected_probe"), (first, "accepted"), (second, "fetch_failed")]
            await run_once(conn, cfg, Secrets(acoustid_key="test"), deps)
            assert source.fetches == 1


async def test_paused_search_does_not_become_missing(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _edition(single=True)
    source = _Source(tmp_path / "must-not-fetch.flac")
    searches = 0

    async def search(_release: Release, _tracks: list[Track]) -> list[Candidate]:
        nonlocal searches
        searches += 1
        raise SourceWall("search is gated")

    monkeypatch.setattr(source, "search", search)
    async with aclosing(_test_http()) as http:
        with closing(open_db(cfg.data_dir / "trawl.sqlite")) as conn:
            _seed(conn, release)
            deps = RunDeps(
                http, _mb(http, conn, monkeypatch, release), [source], AcoustId(http, "test")
            )
            secrets = Secrets(acoustid_key="test")
            report = await run_once(conn, cfg, secrets, deps)
            assert report.states == [("planned", 1)]
            (pause,) = report.pauses
            (retry,) = conn.execute("SELECT next_try_at FROM tracks").fetchone()
            assert pause.reason == "search is gated" and retry == pause.until
            conn.execute("UPDATE tracks SET next_try_at=NULL")
            conn.commit()
            assert (await run_once(conn, cfg, secrets, deps)).states == [("planned", 1)]
            assert searches == 1 and source.fetches == 0


@pytest.mark.tools
@pytest.mark.parametrize("fingerprint_matches", [True, False])
async def test_pin_set_during_fetch_wins_and_a_rejected_pin_is_kept_for_review(
    cfg: Config,
    audio_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fingerprint_matches: bool,
) -> None:
    release = _edition(single=True)
    medium = release.media[0]
    track = replace(medium.tracks[0], length_ms=31_000)
    release = replace(release, media=(replace(medium, tracks=(track,)),))
    pin = tmp_path / "picked.flac"
    shutil.copyfile(audio_file, pin)
    # A separate connection models `trawl pick` while the runner awaits a download.
    db = cfg.data_dir / "trawl.sqlite"

    def pick() -> None:
        with closing(open_db(db)) as writer, writer:
            assert queue.pin(writer, track_id, "path", str(pin))

    recording = track.recording_mbid if fingerprint_matches else None
    async with aclosing(_test_http(recording)) as http:
        with closing(open_db(db)) as conn:
            (track_id,) = _seed(conn, release)
            source = _Source(audio_file, on_fetch=pick)
            deps = RunDeps(
                http, _mb(http, conn, monkeypatch, release), [source], AcoustId(http, "test")
            )
            await run_once(conn, cfg, Secrets(acoustid_key="test"), deps)
            row = conn.execute(
                "SELECT state, verified_by, pinned_candidate, path, next_try_at FROM tracks"
            ).fetchone()
            assert pin.is_file()
            assert source.fetches == 1
            if fingerprint_matches:
                assert tuple(row[:3]) == ("done", "acoustid", None)
                placed = Path(row[3])
                assert await to_thread(placed.is_file)
            else:
                assert row[0] == "missing" and queue.Pin.load(row[2]).ref == str(pin)
                assert row[3] is None and row[4] is None
                rejected = review.rejection(conn, track_id, cfg.library_root)
                assert (
                    rejected is not None and rejected.source == "path" and rejected.path.is_file()
                )
            assert [row[0] for row in conn.execute("SELECT source FROM attempts")] == ["path"]


@pytest.mark.tools
async def test_finalization_retries_placement_without_retagging_and_retries_album_gain(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _edition(single=True)
    track = release_tracks(release)[0]
    inbox = tmp_path / "inbox"
    tone(inbox / "The Band - The Album" / f"01 {track.title}.flac", "flac")
    tools = default_audio_tools()
    writes: list[Path] = []
    gain_calls: list[tuple[Path, ...]] = []
    replace_path = Path.replace

    def write(path: Path, tags: TagFields, cover: bytes | None, lyrics: str | None) -> None:
        writes.append(path)
        tools.write(path, tags, cover, lyrics)

    def locked_replace(path: Path, target: Path) -> Path:
        if target.suffix == ".flac":
            raise PermissionError("library file locked")
        return replace_path(path, target)

    def gain(paths: Sequence[Path]) -> None:
        gain_calls.append(tuple(paths))
        if len(gain_calls) == 1:
            raise AudioError("gain scan failed")
        tools.album_gain(paths)

    secrets = Secrets(acoustid_key="test")
    async with aclosing(_test_http()) as http:
        with closing(open_db(cfg.data_dir / "trawl.sqlite")) as conn:
            _seed(conn, release)
            deps = RunDeps(
                http,
                _mb(http, conn, monkeypatch, release),
                [LocalSource((inbox,))],
                AcoustId(http, "test"),
                replace(tools, write=write, album_gain=gain),
            )
            with monkeypatch.context() as patch:
                patch.setattr(Path, "replace", locked_replace)
                assert (await run_once(conn, cfg, secrets, deps)).states == [("tagged", 1)]
            raw, retry = conn.execute("SELECT path, next_try_at FROM tracks").fetchone()
            staged = Path(raw)
            assert await to_thread(staged.is_file) and retry > queue.now()
            assert writes == [staged] and not gain_calls
            with conn:
                conn.execute("UPDATE tracks SET next_try_at=NULL")
            assert (await run_once(conn, cfg, secrets, deps)).states == [("done", 1)]
            (final,) = queue.done_paths(conn, release.mbid)
            assert await to_thread(final.is_file)
            assert not await to_thread(staged.exists) and writes == [staged]
            done, retry = conn.execute(
                "SELECT album_gain_done, album_gain_next_try_at FROM releases"
            ).fetchone()
            assert not done and retry > queue.now()
            await run_once(conn, cfg, secrets, deps)
            assert gain_calls == [(final,)]
            with conn:
                conn.execute("UPDATE releases SET album_gain_next_try_at=NULL")
            await run_once(conn, cfg, secrets, deps)
            assert gain_calls == [(final,), (final,)]
            assert not queue.gain_releases(conn)
