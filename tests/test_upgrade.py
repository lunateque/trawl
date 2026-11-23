from contextlib import closing
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from test_folder_sources import album, tone

from trawl.config import Config, MbConfig, PlanConfig
from trawl.db import repo
from trawl.db.connect import open_db
from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import Release
from trawl.net.client import HttpClient
from trawl.runner.run import RunDeps
from trawl.runner.upgrade import upgrade_pass
from trawl.sources.local import LocalSource
from trawl.verify.fingerprint import AcoustId


@pytest.mark.tools
@pytest.mark.parametrize("case", ["lossy", "permission", "success"])
async def test_upgrade_uses_only_lossless_and_keeps_locked_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    cfg = Config(
        tmp_path / "library",
        tmp_path / "data",
        "a@b",
        MbConfig(),
        PlanConfig(),
        None,
        lyrics_enabled=False,
    )
    release = replace(album(), media=album().media[:1], genres=("rock",))
    old = cfg.library_root / "01 - First Song.opus"
    tone(old, "libopus")
    original = old.read_bytes()
    inbox = tmp_path / "inbox"
    source_file = (
        inbox
        / "The Band - The Album"
        / ("01 First Song.opus" if case == "lossy" else "01 First Song.flac")
    )
    tone(source_file, "libopus" if case == "lossy" else "flac")

    async def lookup(_: str) -> Release:
        return release

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "coverartarchive.org"
        return httpx.Response(404)

    orig_replace = Path.replace

    def locked_replace(self: Path, target: Path) -> Path:
        if self == old:
            raise PermissionError("locked by player")
        return orig_replace(self, target)

    if case == "permission":
        monkeypatch.setattr(Path, "replace", locked_replace)
    http = HttpClient("fixture", {}, httpx.MockTransport(handle))
    try:
        with closing(open_db(cfg.data_dir / "trawl.sqlite")) as conn:
            with conn:
                repo.upsert_release(conn, release, {})
                medium = release.media[0]
                track, _ = repo.insert_track(conn, release, medium, 1, medium.tracks[0])
                conn.execute(
                    "UPDATE tracks SET state='done',path=?,codec='opus',lossless=0 WHERE id=?",
                    (str(old), track),
                )
            mb = MusicBrainzClient(http, conn, 30)
            monkeypatch.setattr(mb, "release", lookup)
            deps = RunDeps(http, mb, [LocalSource((inbox,), move=True)], AcoustId(http, "fixture"))
            report = await upgrade_pass(conn, cfg, deps, now=True)
            row = conn.execute("SELECT path,codec,lossless FROM tracks").fetchone()
            new = old.with_suffix(".flac")
            assert report.replaced == (case == "success")
            if case == "success":
                assert tuple(row) == (str(new), "flac", 1)
                assert new.is_file() and not old.exists()
            else:
                assert tuple(row) == (str(old), "opus", 0)
                assert old.read_bytes() == original and not new.exists() and source_file.is_file()
            checked, retry = conn.execute(
                "SELECT upgrade_checked_at,upgrade_next_try_at FROM releases"
            ).fetchone()
            assert checked is not None
            assert (retry is not None) == (case == "permission")
            assert (await upgrade_pass(conn, cfg, deps, now=case == "permission")).checked == 0
    finally:
        await http.aclose()
