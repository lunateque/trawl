import asyncio
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from trawl.cli.context import fail, session
from trawl.cli.format import source_report
from trawl.cli.run import process
from trawl.config.models import ConfigError
from trawl.config.secrets import load_secrets
from trawl.db import queue, repo, review
from trawl.db.repo import Kind
from trawl.plan.wants import (
    MbLink,
    parse_add_target,
)
from trawl.runner.lock import LockBusy, acquire
from trawl.tui.data import explain_text


def retry(ctx: typer.Context, skipped: Annotated[bool, typer.Option("--skipped")] = False) -> None:
    """Return missing tracks (and optionally skipped tracks) to the queue."""
    with session(ctx.obj) as (_, conn), conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = queue.retry(conn, skipped=skipped)
    typer.echo(f"Requeued: {changed}")


def pick(ctx: typer.Context, track: int, candidate: str) -> None:
    """Verify a particular YouTube video or local file for a track."""
    with session(ctx.obj) as (cfg, conn):
        try:
            pin = review.parse_pin(candidate, cfg.sources.order)
        except (OSError, ValueError) as exc:
            fail(str(exc), 2)
        if cfg.source_path is None:
            raise ConfigError("Pick requires a configuration file")
        if not load_secrets(cfg.source_path).acoustid_key:
            fail("AcoustID key required for pick; configure acoustid_key in secrets.toml", 1)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if not queue.pin(conn, track, pin.source, pin.ref):
                fail(f"Track {track} cannot be picked in its current state", 1)
        try:
            with acquire(cfg.data_dir):
                asyncio.run(process(cfg, conn, track))
        except LockBusy as exc:
            typer.echo(f"Pinned track {track}; {exc}")
            return
        row = conn.execute("SELECT state FROM tracks WHERE id=?", (track,)).fetchone()
        if row[0] == "missing":
            attempt = conn.execute(
                "SELECT reason FROM attempts WHERE track_id=? ORDER BY id DESC LIMIT 1", (track,)
            ).fetchone()
            fail(f"Track {track} missing: {attempt[0] if attempt else 'candidate rejected'}", 1)


def skip(ctx: typer.Context, target: str) -> None:
    """Skip an unfinished track or the unfinished tracks of a release."""
    try:
        kind, key = _explain_target(target)
        if kind not in {None, Kind.RELEASE}:
            raise ValueError("expected a track ID or release MBID")
    except ValueError as exc:
        fail(str(exc), 2)
    with session(ctx.obj) as (_, conn), conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = queue.skip(conn, key)
    typer.echo(f"Skipped: {changed}")


def status(ctx: typer.Context) -> None:
    """Show track and want counts."""
    with session(ctx.obj) as (_, conn):
        counts = repo.status(conn)
        outcomes = queue.attempt_counts(conn)
        pauses = queue.pauses(conn)
    console = Console()
    tracks = Table("State", "Count", title="Tracks")
    for state, count in counts.tracks:
        tracks.add_row(state, str(count))
    wants = Table("Status", "Origin", "Count", title="Wants")
    for state, origin, count in counts.wants:
        wants.add_row(state, origin, str(count))
    console.print(tracks, wants, sep="\n")
    typer.echo(f"Unresolved: {counts.unresolved}")
    source_report(console, outcomes, pauses)


def _explain_target(target: str) -> tuple[Kind | None, int | str]:
    if target.isascii() and target.isdecimal():
        return None, int(target)
    try:
        return Kind.RELEASE, str(UUID(target))
    except ValueError:
        parsed = parse_add_target(target)
    if isinstance(parsed, MbLink):
        return parsed.kind, parsed.mbid
    raise ValueError("expected a track ID, release MBID, or MusicBrainz link")


def explain(ctx: typer.Context, target: str) -> None:
    """Explain which wants, release decisions and attempts led to a track or album."""
    try:
        kind, value = _explain_target(target)
    except ValueError as exc:
        fail(str(exc), 2)
    with session(ctx.obj) as (_, conn):
        explanation = repo.explain(conn, kind, value)
    if not explanation.releases:
        fail(f"No planned tracks match {target}", 1)
    typer.echo(explain_text(explanation))
