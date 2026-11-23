import json
import sqlite3
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from trawl import cli
from trawl.config import Config
from trawl.db.connect import open_db
from trawl.mb.client import MusicBrainzClient
from trawl.net.client import HttpClient

MULTI = "541a0976-ca45-3c0f-89e5-26bc376f58d1"
KINO = "064db6e8-fdfb-4acb-a327-fc2de75b37de"


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f"library_root = {json.dumps((tmp_path / 'library').as_posix())}\n"
        f"data_dir = {json.dumps((tmp_path / 'data').as_posix())}\n"
        'contact_email = "test@example.com"\n',
        encoding="utf-8",
    )
    return path


def test_add_plan_status_explain(
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
    mb_fixture: Callable[[str], dict[str, Any]],
) -> None:
    runner = CliRunner()
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        assert req.url.path == f"/ws/2/release/{MULTI}"
        return httpx.Response(200, json=mb_fixture("multi_release"))

    @asynccontextmanager
    async def make_mb(_: Config, db: sqlite3.Connection) -> AsyncGenerator[MusicBrainzClient]:
        http = HttpClient("test", {}, httpx.MockTransport(handler))
        try:
            yield MusicBrainzClient(http, db, 30)
        finally:
            await http.aclose()

    monkeypatch.setattr(cli, "make_mb", make_mb)
    args = ["--config", str(config)]
    added = runner.invoke(cli.app, [*args, "add", f"https://musicbrainz.org/release/{MULTI}"])
    assert added.exit_code == 0, added.output
    assert "Want 1" in added.output
    assert calls == []
    planned = runner.invoke(cli.app, [*args, "plan"])
    assert planned.exit_code == 0, planned.output
    assert "12 tracks" in planned.output
    assert calls == [f"/ws/2/release/{MULTI}"]
    status = runner.invoke(cli.app, [*args, "status"])
    assert status.exit_code == 0, status.output
    assert all(text in status.output for text in ("planned", "12", "manual", "Unresolved: 0"))
    with closing(open_db(config.parent / "data" / "trawl.sqlite")) as db, db:
        db.execute(
            """INSERT INTO attempts (track_id,source,candidate_ref,outcome,reason,at)
            VALUES (1,'test','reference','fetch_failed','failure reason','2026-11-23T08:00:00Z')"""
        )
    for target in ("1", MULTI, f"https://musicbrainz.org/release/{MULTI}"):
        explanation = runner.invoke(cli.app, [*args, "explain", target])
        assert explanation.exit_code == 0, explanation.output
        assert all(
            text in explanation.output
            for text in (
                "choice_reason",
                "explicit release",
                "manual",
                "Attempts",
                "fetch_failed",
                "failure reason",
            )
        )


def test_text_selection(
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
    mb_fixture: Callable[[str], dict[str, Any]],
) -> None:
    runner = CliRunner()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/artist"):
            return httpx.Response(200, json=mb_fixture("kino_search"))
        body = mb_fixture("ok_search")
        body["release-groups"][0]["score"] = 89
        return httpx.Response(200, json=body)

    @asynccontextmanager
    async def make_mb(_: Config, db: sqlite3.Connection) -> AsyncGenerator[MusicBrainzClient]:
        http = HttpClient("test", {}, httpx.MockTransport(handler))
        try:
            yield MusicBrainzClient(http, db, 30)
        finally:
            await http.aclose()

    monkeypatch.setattr(cli, "make_mb", make_mb)
    args = ["--config", str(config), "add"]
    kino = runner.invoke(cli.app, [*args, "Кино"])
    assert kino.exit_code == 0, kino.output
    assert "Want 1" in kino.output
    cancelled = runner.invoke(cli.app, [*args, "Radiohead - OK Computer"], input="0\n")
    assert cancelled.exit_code == 0, cancelled.output
    assert "Cancelled" in cancelled.output and "89" in cancelled.output
    yes = runner.invoke(cli.app, [*args, "Radiohead - OK Computer", "--yes"])
    assert yes.exit_code == 0, yes.output
    assert "Want 2" in yes.output
    with closing(open_db(config.parent / "data" / "trawl.sqlite")) as db:
        row = db.execute('SELECT mbid, scope, "limit" FROM wants WHERE id = 1').fetchone()
        assert tuple(row) == (KINO, "discography", 14)


def test_foreign_link_is_rejected_without_configuration() -> None:
    result = CliRunner().invoke(cli.app, ["add", "https://open.spotify.com/track/123"])
    assert result.exit_code == 2
    assert (
        "links from Spotify are supported from step 5; use a MusicBrainz link or text"
        in result.output
    )


def test_doctor_offline(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_: str) -> None:
        return None

    monkeypatch.setattr("trawl.doctor.shutil.which", missing)
    result = CliRunner().invoke(cli.app, ["--config", str(config), "doctor", "--offline"])
    assert result.exit_code == 1, result.output
    assert all(
        text in result.output
        for text in ("FAIL", "ffmpeg", "fpcalc", "rsgain", "offline", "AcoustID")
    )
    assert (config.parent / "library" / ".trawl-staging").is_dir()


def test_bad_config_exits_with_dotted_key(config: Path) -> None:
    with config.open("a", encoding="utf-8") as stream:
        stream.write("\n[plan]\nprefr = 'original'\n")
    for command in (["status"], ["doctor", "--offline"]):
        result = CliRunner().invoke(cli.app, ["--config", str(config), *command])
        assert result.exit_code == 1, result.output
        assert "plan.prefr" in result.output
