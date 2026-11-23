import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, closing, nullcontext, suppress
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from trawl import __version__
from trawl.cli.context import fail, make_mb, session
from trawl.cli.format import source_report
from trawl.cli.taste import sync
from trawl.config.models import Config, ConfigError, Secrets
from trawl.config.secrets import load_secrets
from trawl.db import queue
from trawl.db.connect import open_db
from trawl.mb.client import MusicBrainzClient
from trawl.net.client import HttpClient
from trawl.net.limiter import RateLimiter
from trawl.plan.run import plan_new_wants
from trawl.runner.context import RunDeps
from trawl.runner.lock import LockBusy, acquire
from trawl.runner.progress import Progress
from trawl.runner.run import run_once
from trawl.runner.upgrade import upgrade_pass
from trawl.runner.watch import Job, graceful_stop
from trawl.runner.watch import watch as watch_jobs
from trawl.sources.base import Source
from trawl.sources.local import LocalSource
from trawl.sources.soulseek import SoulseekSource
from trawl.sources.ytmusic import YTMusic, YTMusicSource
from trawl.taste.sync import TasteSource
from trawl.verify.fingerprint import AcoustId


@asynccontextmanager
async def run_dependencies(
    cfg: Config, conn: sqlite3.Connection
) -> AsyncGenerator[tuple[Secrets, RunDeps]]:
    if cfg.source_path is None:
        raise ConfigError("Processing requires a configuration file")
    secrets = load_secrets(cfg.source_path)
    if not (key := secrets.acoustid_key):
        raise ConfigError(
            "Configure acoustid_key in secrets.toml (https://acoustid.org/new-application)"
        )
    http = HttpClient(
        f"trawl/{__version__} ( {cfg.contact_email} )",
        {
            host: RateLimiter(interval)
            for host, interval in (
                ("musicbrainz.org", 1.0),
                ("api.acoustid.org", 1 / 3),
                ("lrclib.net", 0.3),
                ("coverartarchive.org", 0.2),
            )
        },
        proxy=cfg.proxy,
    )
    try:
        sources: list[Source] = []
        for name in cfg.sources.order:
            if name == "ytmusic":
                sources.append(YTMusicSource(YTMusic(), None, secrets.youtube_cookies))
            elif name == "local":
                local = cfg.sources.local
                sources.append(
                    LocalSource(
                        local.inbox, move=local.move, lossless_repack=cfg.sources.lossless_repack
                    )
                )
            elif name == "soulseek":
                slskd = cfg.sources.soulseek
                if slskd.download_dir is None:
                    raise ConfigError("sources.soulseek.download_dir: required")
                sources.append(
                    SoulseekSource(
                        http,
                        secrets.slskd_url,
                        secrets.slskd_api_key,
                        slskd.download_dir,
                        concurrency=slskd.concurrency,
                        search_timeout_s=slskd.search_timeout_s,
                        stall_timeout_s=slskd.stall_timeout_s,
                        candidate_timeout_s=slskd.candidate_timeout_s,
                        allow_insecure=secrets.slskd_allow_insecure,
                        lossless_repack=cfg.sources.lossless_repack,
                    )
                )
        yield (
            secrets,
            RunDeps(
                http, MusicBrainzClient(http, conn, cfg.mb.cache_days), sources, AcoustId(http, key)
            ),
        )
    finally:
        await http.aclose()


async def process(
    cfg: Config,
    conn: sqlite3.Connection,
    only: int | None = None,
    *,
    stop: asyncio.Event | None = None,
    progress: Progress | None = None,
) -> None:
    async with run_dependencies(cfg, conn) as (secrets, deps):
        report = await run_once(conn, cfg, secrets, deps, only=only, stop=stop, progress=progress)
    if progress is None:
        show_run(report.states, report.outcomes, report.pauses)


def show_run(
    states: list[tuple[str, int]], outcomes: list[tuple[str, int, int]], pauses: list[queue.Pause]
) -> None:
    console = Console()
    table = Table("State", "Count", title="Tracks")
    for state, count in states:
        table.add_row(state, str(count))
    console.print(table)
    source_report(console, outcomes, pauses)


def run(
    ctx: typer.Context,
    *,
    once: Annotated[bool, typer.Option("--once", help="Process the current queue once")] = False,
    watch: Annotated[bool, typer.Option("--watch", help="Process the queue on a schedule")] = False,
) -> None:
    """Process the current queue."""
    if once and watch:
        fail("Choose either --once or --watch", 2)
    with session(ctx.obj) as (cfg, conn):
        try:
            with acquire(cfg.data_dir):
                asyncio.run(_watch(cfg, conn) if watch else process(cfg, conn))
        except LockBusy as exc:
            fail(str(exc), 3)


async def _watch(
    cfg: Config,
    conn: sqlite3.Connection,
    stop: asyncio.Event | None = None,
    progress: Progress | None = None,
) -> None:
    cli_signals = stop is None
    stop = stop if stop is not None else asyncio.Event()

    async def taste(source: TasteSource) -> None:
        async with make_mb(cfg, conn) as mb:
            await sync(conn, mb, cfg, source, releases=False, terminal=progress is None)

    async def planning() -> None:
        async with make_mb(cfg, conn) as mb:
            report = await plan_new_wants(conn, mb, cfg.plan)
        if report.errors:
            raise ConfigError(f"{report.errors} wants failed to plan")

    async def retry_due() -> None:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            queue.retry(conn, skipped=False, due=queue.now())

    with graceful_stop(stop) if cli_signals else nullcontext():
        await watch_jobs(
            conn,
            [
                Job("taste", cfg.watch.taste_every, lambda: taste(TasteSource.ALL)),
                Job("releases", cfg.watch.releases_every, lambda: taste(TasteSource.RELEASES)),
                Job("plan", cfg.watch.queue_every, planning),
                Job(
                    "run",
                    cfg.watch.queue_every,
                    lambda: process(cfg, conn, stop=stop, progress=progress),
                ),
                Job("retry", cfg.watch.queue_every, retry_due),
                Job(
                    "upgrade",
                    cfg.watch.upgrade_every,
                    lambda: _upgrade(cfg, conn, False, stop=stop, progress=progress),
                ),
            ],
            stop,
        )


def tui(ctx: typer.Context) -> None:
    """Monitor the queue and make manual decisions in the terminal."""
    from trawl.tui.app import TrawlApp

    with session(ctx.obj, terminal=False) as (cfg, conn):

        async def embedded(stop: asyncio.Event, progress: Progress) -> None:
            with closing(open_db(cfg.data_dir / "trawl.sqlite")) as runner_conn:
                await _watch(cfg, runner_conn, stop, progress)

        async def launch() -> None:
            dashboard = TrawlApp(cfg, conn, embedded)
            interrupt = asyncio.Event()

            async def close_on_signal() -> None:
                await interrupt.wait()
                await dashboard.action_quit()

            with graceful_stop(interrupt):
                task = asyncio.create_task(close_on_signal())
                try:
                    await dashboard.run_async()
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

        asyncio.run(launch())


async def _upgrade(
    cfg: Config,
    conn: sqlite3.Connection,
    now: bool,
    *,
    stop: asyncio.Event | None = None,
    progress: Progress | None = None,
) -> None:
    async with run_dependencies(cfg, conn) as (_, deps):
        report = await upgrade_pass(conn, cfg, deps, now=now, stop=stop, progress=progress)
    if progress is None:
        typer.echo(
            f"Releases checked: {report.checked}; "
            f"tracks upgraded: {report.replaced}; deferred: {report.deferred}"
        )


def upgrade(ctx: typer.Context, now: Annotated[bool, typer.Option("--now")] = False) -> None:
    """Replace verified lossy library audio with lossless files."""
    with session(ctx.obj) as (cfg, conn):
        try:
            with acquire(cfg.data_dir):
                asyncio.run(_upgrade(cfg, conn, now))
        except LockBusy as exc:
            fail(str(exc), 3)
