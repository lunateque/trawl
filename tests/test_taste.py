import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, closing
from pathlib import Path

import httpx
import pytest
from conftest import mbid
from typer.testing import CliRunner

from trawl.cli import app
from trawl.config.models import Config
from trawl.db.connect import open_db
from trawl.mb.client import MusicBrainzClient
from trawl.net.client import HttpClient


def test_link_consumes_unresolved_and_keeps_origin(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'contact_email="a@b"\nlibrary_root="{(tmp_path / "lib").as_posix()}"\n'
        f'data_dir="{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )
    with open_db(tmp_path / "trawl.sqlite") as conn:
        conn.execute("""INSERT INTO unresolved(origin,origin_ref,title,reason,last_try_at)
                     VALUES ('spotify','track:id','Song','no match','today')""")
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(cfg),
            "link",
            "1",
            "https://musicbrainz.org/recording/3b7c026a-4256-4448-b234-abcdef000001",
        ],
    )
    assert result.exit_code == 0, result.output
    with open_db(tmp_path / "trawl.sqlite") as conn:
        assert tuple(conn.execute("SELECT kind,scope,origin,origin_ref FROM wants").fetchone()) == (
            "recording",
            "track",
            "spotify",
            "track:id",
        )
        assert conn.execute("SELECT count(*) FROM unresolved").fetchone()[0] == 0


def test_watch_and_auth_are_real_cli_options() -> None:
    runner = CliRunner()
    run = runner.invoke(app, ["run", "--help"])
    assert "Available from step 6" not in run.output
    assert runner.invoke(app, ["auth", "spotify", "--help"]).exit_code == 0
    assert runner.invoke(app, ["service", "install", "--help"]).exit_code == 0


def test_youtube_add_resolves_playlist_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'contact_email="a@b"\nlibrary_root="{(tmp_path / "lib").as_posix()}"\n'
        f'data_dir="{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )

    class YouTube:
        def get_playlist(self, identifier: str, limit: int | None = None) -> object:
            assert identifier == "PLfixture"
            assert limit is None
            return {
                "tracks": [
                    {
                        "videoId": "match",
                        "title": "Song",
                        "artists": [{"name": "Artist"}],
                        "duration_seconds": 200,
                    },
                    {
                        "videoId": "unmatched",
                        "title": "Not indexed",
                        "artists": [{"name": "Artist"}],
                    },
                ]
            }

    def response(req: httpx.Request) -> httpx.Response:
        query = req.url.params["query"]
        assert req.url.path == "/ws/2/recording"
        return httpx.Response(
            200,
            json={
                "recordings": [
                    {
                        "id": mbid("matched-mbid"),
                        "title": "Song",
                        "length": 200_000,
                        "score": 99,
                        "artist-credit": [
                            {"name": "Artist", "artist": {"id": mbid("artist"), "name": "Artist"}}
                        ],
                    }
                ]
                if 'recording:"Song"' in query
                else []
            },
        )

    @asynccontextmanager
    async def musicbrainz(
        config: Config, conn: sqlite3.Connection
    ) -> AsyncGenerator[MusicBrainzClient]:
        http = HttpClient("test", {}, httpx.MockTransport(response))
        try:
            yield MusicBrainzClient(http, conn, config.mb.cache_days)
        finally:
            await http.aclose()

    monkeypatch.setattr("trawl.taste.links.YTMusic", YouTube)
    monkeypatch.setattr("trawl.cli.add.make_mb", musicbrainz)
    cmd = ["--config", str(cfg), "add", "https://music.youtube.com/playlist?list=PLfixture"]
    first = CliRunner().invoke(app, cmd)
    assert first.exit_code == 0, first.output
    assert "Wants: 1; unresolved: 1" in first.output
    second = CliRunner().invoke(app, cmd)
    assert second.exit_code == 0, second.output
    assert "Wants: 0; unresolved: 1" in second.output
    with closing(open_db(tmp_path / "trawl.sqlite")) as conn:
        assert tuple(conn.execute("SELECT mbid,origin,scope FROM wants").fetchone()) == (
            mbid("matched-mbid"),
            "manual",
            "track",
        )
        assert conn.execute("SELECT count(*) FROM unresolved").fetchone()[0] == 1
