"""Seed a throwaway trawl configuration and capture two offline TUI screenshots."""

import argparse
import sys
import tempfile
from contextlib import closing
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from textual.app import App
from textual.pilot import Pilot
from typer.testing import CliRunner

from trawl.cli import app
from trawl.config.load import load
from trawl.config.models import Config
from trawl.core.fetch import Candidate, CandidateItem, Fetched
from trawl.db import queue, review
from trawl.db.connect import open_db
from trawl.db.states import TrackState
from trawl.library.place import staging_dir
from trawl.runner.lock import acquire


def seed(root: Path) -> Config:
    config = root / "config.toml"
    root.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'contact_email="tui@example.test"\ndata_dir="{(root / "data").as_posix()}"\n'
        f'library_root="{(root / "library").as_posix()}"\n'
        '[sources]\norder=["ytmusic", "local"]\n',
        encoding="utf-8",
    )
    cfg = load(config)
    with closing(open_db(cfg.data_dir / "trawl.sqlite")) as db, db:
        db.execute(
            """INSERT INTO releases(mbid,release_group_mbid,title,artist_credit,
               disc_count,choice_reason)
               VALUES ('demo','group','Demo Album','Demo Artist',1,'{}')"""
        )
        names = (
            "Incoming",
            "Looking for audio",
            "Downloaded",
            "Verified",
            "Tagged",
            "In library",
            "Lost Song",
            "Skipped",
            "Rejected take",
            "Second missing",
        )
        states = (*TrackState, TrackState.MISSING, TrackState.MISSING)
        tracks = [
            (i, f"release-track-{i}", f"recording-{i}", i, name, state)
            for i, (name, state) in enumerate(zip(names, states, strict=True), 1)
        ]
        db.executemany(
            """INSERT INTO tracks(id,release_mbid,release_track_mbid,recording_mbid,disc,
               disc_track_count,position,title,artist_credit,artist_mbids,state,length_ms)
               VALUES (?,'demo',?,?,1,10,?,?,'Demo Artist','[]',?,213000)""",
            tracks,
        )
        item = CandidateItem(
            "Demo Artist/Demo Album/07 Lost Song.flac",
            7,
            213000,
            "FLAC rip",
            size=1100,
        )
        candidate = Candidate(
            "soulseek",
            "forest-peer",
            "album",
            "Lossless folder",
            "flac",
            None,
            True,
            False,
            (item,),
            score=0.92,
        )
        review.remember(db, 7, candidate, item)
        queue.attempt(
            db,
            7,
            "soulseek",
            item.ref,
            0.92,
            "fetch_failed",
            "transfer stalled",
            candidate_key=candidate.ref,
        )
        item = CandidateItem("a1b2c3d4e5f", 7, 213000, "Official upload")
        candidate = Candidate(
            "ytmusic",
            item.ref,
            "track",
            item.title,
            "opus",
            None,
            False,
            True,
            (item,),
            score=0.83,
        )
        review.remember(db, 7, candidate, item)
        staged = staging_dir(cfg.library_root) / "rejected.flac"
        staged.write_bytes(b"offline TUI fixture")
        review.reject(
            db,
            9,
            "ytmusic",
            "another-take",
            0.86,
            "AcoustID matched a different recording",
            Fetched("another-take", staged, "flac", 960_000, True),
            "comparison-hit (0.98)",
        )
        queue.attempt(db, 6, "ytmusic", "library-take", 0.96, "accepted", "acoustid")
        queue.wall(db, "soulseek", "connection refused")
        db.execute(
            """INSERT INTO unresolved(origin,origin_ref,artist,title,album,reason,last_try_at)
               VALUES ('spotify','track:777','Unknown Artist','External Song',
                       'Unmapped Album','no recording match',?)""",
            (queue.now(),),
        )
    return cfg


def capture(cfg: Config, root: Path) -> None:
    width, height = 136, 23
    run_async = App[None].run_async

    def save(dashboard: App[None], name: str) -> None:
        dashboard.save_screenshot(f"{name}.svg", path=str(root))
        console = Console(
            width=width,
            height=height,
            file=StringIO(),
            record=True,
            force_terminal=True,
            legacy_windows=False,
            safe_box=False,
        )
        compositor = dashboard.screen._compositor  # pyright: ignore[reportPrivateUsage]
        backgrounds = dashboard.screen_stack[:-1]
        console.print(
            compositor.render_update(  # pyright: ignore[reportUnknownMemberType]
                full=True,
                screen_stack=backgrounds,
            ),
            end="",
        )
        (root / f"{name}.txt").write_text(console.export_text(), encoding="utf-8")

    async def piloted(dashboard: App[None]) -> None:
        async def screenshot(pilot: Pilot[object]) -> None:
            await pilot.pause()
            save(dashboard, "dashboard")
            await pilot.press("p")
            save(dashboard, "candidates")
            await pilot.press("escape", "q")

        await run_async(dashboard, headless=True, size=(width, height), auto_pilot=screenshot)

    with acquire(cfg.data_dir), patch.object(App, "run_async", piloted):
        result = CliRunner().invoke(app, ["--config", str(cfg.source_path), "tui"])
    if result.exception is not None:
        raise result.exception


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path)
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    directory = args.directory or Path(tempfile.mkdtemp(prefix="trawl-tui-"))
    cfg = seed(directory)
    if args.capture:
        capture(cfg, directory)
    sys.stdout.write(f"{directory}\n")
