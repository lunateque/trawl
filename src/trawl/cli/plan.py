import asyncio
import sqlite3
from pathlib import Path
from typing import Annotated

import typer

from trawl.cli.context import fail, make_mb, session
from trawl.cli.run import run_dependencies, show_run
from trawl.config.models import Config
from trawl.plan.import_folder import plan_import
from trawl.plan.run import plan_new_wants
from trawl.runner.lock import LockBusy, acquire
from trawl.runner.run import run_once
from trawl.verify.probe import AudioError


def plan(ctx: typer.Context, refresh: Annotated[bool, typer.Option("--refresh")] = False) -> None:
    """Turn new wants into planned releases and tracks."""
    with session(ctx.obj) as (cfg, conn):

        async def run() -> None:
            async with make_mb(cfg, conn) as mb:
                mb.refresh = refresh
                report = await plan_new_wants(conn, mb, cfg.plan)
            typer.echo(
                f"Wants: {report.planned} planned, {report.empty} empty, {report.errors} failed; "
                f"{report.tracks} tracks added"
            )
            if report.errors:
                raise typer.Exit(1)

        asyncio.run(run())


async def _import_folder(cfg: Config, conn: sqlite3.Connection, directory: Path) -> None:
    async with run_dependencies(cfg, conn) as (secrets, deps):
        release = await plan_import(conn, cfg, deps.mb, deps.acoustid, directory)
        typer.echo(f"Import: {release.title} ({release.mbid})")
        report = await run_once(conn, cfg, secrets, deps, release_mbid=release.mbid)
    show_run(report.states, report.outcomes, report.pauses)


def import_command(
    ctx: typer.Context,
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
) -> None:
    """Identify an album folder by fingerprint and import its files."""
    with session(ctx.obj) as (cfg, conn):
        try:
            with acquire(cfg.data_dir):
                asyncio.run(_import_folder(cfg, conn, directory))
        except LockBusy as exc:
            fail(str(exc), 3)
        except (AudioError, ValueError) as exc:
            fail(str(exc), 1)
