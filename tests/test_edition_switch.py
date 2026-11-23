import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from test_folder_sources import album
from typer.testing import CliRunner

from trawl.cli import app
from trawl.db import repo
from trawl.db.connect import open_db
from trawl.db.repo import Kind, Origin, Scope
from trawl.mb.models import Track
from trawl.plan.switch import switch_edition
from trawl.plan.wants import create_want


@pytest.mark.parametrize("done", [False, True])
def test_switch_preserves_want_links_and_explains(tmp_path: Path, done: bool) -> None:
    old = replace(album(), mbid="3b7c026a-4256-4448-b234-abcdef000001")
    first = replace(old.media[0].tracks[0], mbid="new-first")
    third = Track("new-third", "rec-3", 2, "2", "Bonus", 4000, old.credits, ())
    new = replace(
        old,
        mbid="3b7c026a-4256-4448-b234-abcdef000002",
        media=(replace(old.media[0], tracks=(first, third)),),
    )
    config = tmp_path / "config.toml"
    config.write_text(
        f'contact_email="a@b"\ndata_dir="{tmp_path.as_posix()}"\n'
        f'library_root="{(tmp_path / "library").as_posix()}"\n'
    )
    with closing(open_db(tmp_path / "trawl.sqlite")) as conn:
        with conn:
            repo.upsert_release(conn, old, {"selected_by": "preferred edition"})
            want = create_want(
                conn, Kind.RELEASE_GROUP, old.release_group.mbid, Scope.ALBUM, Origin.MANUAL, "test"
            )
            recording_want = create_want(
                conn, Kind.RECORDING, first.recording_mbid, Scope.TRACK, Origin.MANUAL, "liked"
            )
            for medium in old.media:
                track, _ = repo.insert_track(conn, old, medium, 1, medium.tracks[0])
                repo.link_track(conn, want, track)
                if medium.position == 1:
                    repo.link_track(conn, recording_want, track)
                    if done:
                        conn.execute("UPDATE tracks SET state='done' WHERE id=?", (track,))
        assert switch_edition(conn, old, new, "local folder fits better") is not done
        rows = list(
            conn.execute(
                "SELECT release_mbid,release_track_mbid FROM tracks ORDER BY release_track_mbid"
            )
        )
        if done:
            assert [tuple(row) for row in rows] == [(old.mbid, "track-1"), (old.mbid, "track-2")]
            return
        assert [tuple(row) for row in rows] == [(new.mbid, "new-first"), (new.mbid, "new-third")]
        linked = list(
            conn.execute(
                """SELECT want_id,release_track_mbid FROM want_tracks
                JOIN tracks ON id=track_id ORDER BY want_id,release_track_mbid"""
            )
        )
        assert [tuple(row) for row in linked] == [
            (want, "new-first"),
            (want, "new-third"),
            (recording_want, "new-first"),
        ]
        reason = json.loads(
            conn.execute("SELECT choice_reason FROM releases WHERE mbid=?", (new.mbid,)).fetchone()[
                0
            ]
        )
        assert reason["switched_from"] == old.mbid
    result = CliRunner().invoke(app, ["--config", str(config), "explain", old.mbid])
    assert result.exit_code == 0, result.output
    assert "switched_reason" in result.output and new.mbid in result.output
