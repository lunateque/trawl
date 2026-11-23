import asyncio
import sqlite3
from enum import StrEnum

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from trawl.cli.context import Options, fail, make_mb, session
from trawl.config.models import Config, ConfigError
from trawl.db.repo import Kind
from trawl.mb.client import MusicBrainzClient
from trawl.plan.wants import (
    MbLink,
    TextArtist,
    parse_add_target,
)
from trawl.resolve.match import resolve_artist
from trawl.taste.oauth import auth_spotify
from trawl.taste.releases import save_artist
from trawl.taste.store import bind
from trawl.taste.sync import TasteSource, read_secrets, sync_taste, taste_http


async def sync(
    conn: sqlite3.Connection,
    mb: MusicBrainzClient,
    cfg: Config,
    source: TasteSource,
    *,
    releases: bool = True,
    terminal: bool = True,
) -> None:
    results = await sync_taste(conn, mb, cfg, source, releases=releases)
    if terminal:
        for name, report, _ in results:
            detail = f"; {report.message}" if report.message else ""
            typer.echo(f"{name}: {report.wants} wants; {report.unresolved} unresolved{detail}")
    if any(not ok for _, _, ok in results):
        if terminal:
            raise typer.Exit(1)
        raise ConfigError("taste synchronization failed")


def sync_taste_command(ctx: typer.Context, source: TasteSource = TasteSource.ALL) -> None:
    """Sync listening history, likes, playlists, or followed releases."""
    with session(ctx.obj) as (cfg, conn):

        async def execute() -> None:
            async with make_mb(cfg, conn) as mb:
                await sync(conn, mb, cfg, source)

        asyncio.run(execute())


def link(ctx: typer.Context, unresolved_id: int, target: str) -> None:
    """Resolve an external item with a MusicBrainz link."""
    try:
        parsed = parse_add_target(target)
    except ValueError as exc:
        fail(str(exc), 2)
    if not isinstance(parsed, MbLink):
        fail("Expected a MusicBrainz link", 2)
    with session(ctx.obj) as (cfg, conn):
        want_id = bind(conn, unresolved_id, parsed, cfg.plan.discography_limit)
    if want_id is None:
        fail(f"Unresolved item {unresolved_id} does not exist", 1)
    typer.echo(f"Want {want_id}: {parsed.kind} {parsed.mbid}")


def unresolved(ctx: typer.Context) -> None:
    """List external items not matched to MusicBrainz."""
    with session(ctx.obj) as (_, conn):
        rows = conn.execute(
            """SELECT id,origin,artist,title,album,origin_ref,reason
            FROM unresolved ORDER BY id"""
        ).fetchall()
    table = Table("ID", "Origin", "Artist", "Title", "Album", "Reference", "Reason")
    for row in rows:
        table.add_row(*(Text(str(value if value is not None else "")) for value in row))
    Console().print(table)


def _follow(ctx: typer.Context, target: str, followed: bool) -> None:
    try:
        parsed = parse_add_target(target)
    except ValueError as exc:
        fail(str(exc), 2)
    if not (
        isinstance(parsed, TextArtist)
        or (isinstance(parsed, MbLink) and parsed.kind == Kind.ARTIST)
    ):
        fail("Expected a MusicBrainz artist link or name", 2)
    with session(ctx.obj) as (cfg, conn):

        async def execute() -> None:
            async with make_mb(cfg, conn) as mb:
                artist = (
                    await mb.artist(parsed.mbid)
                    if isinstance(parsed, MbLink)
                    else await resolve_artist(mb, parsed.name)
                )
            if artist is None:
                fail("No exact MusicBrainz artist found; use an artist link", 1)
            with conn:
                save_artist(conn, artist, followed=followed, manual=True)
            typer.echo(f"{'Following' if followed else 'Unfollowed'}: {artist.name}")

        asyncio.run(execute())


def follow(ctx: typer.Context, artist: str) -> None:
    """Follow new releases by a MusicBrainz artist."""
    _follow(ctx, artist, True)


def unfollow(ctx: typer.Context, artist: str) -> None:
    """Stop following an artist's new releases."""
    _follow(ctx, artist, False)


class AuthProvider(StrEnum):
    SPOTIFY = "spotify"


def auth(ctx: typer.Context, provider: AuthProvider) -> None:
    """Authorize a taste provider in your browser."""
    opts: Options = ctx.obj
    with session(opts) as (cfg, _):
        if not (client_id := read_secrets(cfg).spotify_client_id):
            fail("Configure spotify_client_id in secrets.toml first", 1)

        async def execute() -> None:
            async with taste_http(cfg) as http:
                await auth_spotify(http, client_id, opts.config.with_name("secrets.toml"))

        asyncio.run(execute())
    typer.echo(f"Authorized {provider}")
