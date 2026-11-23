import sqlite3
from collections.abc import AsyncGenerator, Generator, Sequence
from contextlib import asynccontextmanager, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Never

import typer

from trawl import __version__, log
from trawl.config.load import config_path, load
from trawl.config.models import Config, ConfigError
from trawl.core.catalog import MbDataError
from trawl.db.connect import open_db
from trawl.mb.client import MusicBrainzClient
from trawl.net.client import HttpClient, HttpError
from trawl.net.limiter import RateLimiter


@dataclass(frozen=True)
class Options:
    config: Path
    verbose: bool


def options(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option("--config", help="Configuration file")] = None,
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Show debug logging")] = False,
) -> None:
    ctx.obj = Options(config_path(config), verbose)


def fail(message: str, code: int) -> Never:
    typer.echo(message, err=True)
    raise typer.Exit(code)


@contextmanager
def session(
    options: Options, *, terminal: bool = True
) -> Generator[tuple[Config, sqlite3.Connection]]:
    try:
        cfg = load(options.config)
        with (
            log.setup(cfg.data_dir, options.verbose, terminal=terminal),
            closing(open_db(cfg.data_dir / "trawl.sqlite")) as conn,
        ):
            yield cfg, conn
    except (ConfigError, OSError, sqlite3.Error, HttpError, MbDataError) as exc:
        fail(str(exc), 1)


@asynccontextmanager
async def make_mb(cfg: Config, conn: sqlite3.Connection) -> AsyncGenerator[MusicBrainzClient]:
    http = HttpClient(
        f"trawl/{__version__} ( {cfg.contact_email} )",
        {"musicbrainz.org": RateLimiter(1.0)},
        proxy=cfg.proxy,
    )
    try:
        yield MusicBrainzClient(http, conn, cfg.mb.cache_days)
    finally:
        await http.aclose()


def select_candidate(candidates: Sequence[str]) -> int | None:
    for number, row in enumerate(candidates, 1):
        typer.echo(f"{number}. {row}")
    while True:
        selected: int = typer.prompt("Select a candidate (0 cancels)", type=int, default=0)
        if 0 <= selected <= len(candidates):
            return selected - 1 if selected else None
        typer.echo(f"Enter a number from 0 to {len(candidates)}", err=True)
