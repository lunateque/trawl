import asyncio
import sqlite3
from collections.abc import AsyncGenerator, Generator, Sequence
from contextlib import asynccontextmanager, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Never
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from trawl import __version__, doctor, log
from trawl.config import Config, ConfigError, config_path, load
from trawl.db import repo
from trawl.db.connect import open_db
from trawl.db.repo import Kind, Origin, Scope
from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import MbDataError
from trawl.net.client import HttpClient, HttpError
from trawl.net.limiter import RateLimiter
from trawl.normalize import norm_artist, norm_title
from trawl.plan.run import plan_new_wants
from trawl.plan.wants import (
    ForeignLink,
    MbLink,
    TextAlbum,
    TextArtist,
    create_want,
    parse_add_target,
)

app = typer.Typer(help="Plan a tagged local music library with MusicBrainz.", no_args_is_help=True)


@dataclass(frozen=True)
class Options:
    config: Path
    verbose: bool


@app.callback()
def options(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option("--config", help="Configuration file")] = None,
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Show debug logging")] = False,
) -> None:
    ctx.obj = Options(config_path(config), verbose)


def _fail(message: str, code: int) -> Never:
    typer.echo(message, err=True)
    raise typer.Exit(code)


@contextmanager
def _session(options: Options) -> Generator[tuple[Config, sqlite3.Connection]]:
    try:
        cfg = load(options.config)
        with (
            log.setup(cfg.data_dir, options.verbose),
            closing(open_db(cfg.data_dir / "trawl.sqlite")) as conn,
        ):
            yield cfg, conn
    except (ConfigError, OSError, sqlite3.Error, HttpError, MbDataError) as exc:
        _fail(str(exc), 1)


@asynccontextmanager
async def make_mb(cfg: Config, conn: sqlite3.Connection) -> AsyncGenerator[MusicBrainzClient]:
    http = HttpClient(
        f"trawl/{__version__} ( {cfg.contact_email} )", {"musicbrainz.org": RateLimiter(1.0)}
    )
    try:
        yield MusicBrainzClient(http, conn, cfg.mb.cache_days)
    finally:
        await http.aclose()


def _pick(candidates: Sequence[str]) -> int | None:
    for number, row in enumerate(candidates, 1):
        typer.echo(f"{number}. {row}")
    while True:
        selected: int = typer.prompt("Select a candidate (0 cancels)", type=int, default=0)
        if 0 <= selected <= len(candidates):
            return selected - 1 if selected else None
        typer.echo(f"Enter a number from 0 to {len(candidates)}", err=True)


async def _resolve(
    cfg: Config,
    conn: sqlite3.Connection,
    target: TextAlbum | TextArtist,
    yes: bool,
) -> MbLink | None:
    async with make_mb(cfg, conn) as mb:
        if isinstance(target, TextAlbum):
            albums = await mb.search_release_groups(target.artist, target.title)
            if not albums:
                _fail("No MusicBrainz release groups found", 1)
            group, score, artist = albums[0]
            if yes or (
                score >= 90
                and norm_artist(artist) == norm_artist(target.artist)
                and norm_title(group.title) == norm_title(target.title)
            ):
                return MbLink(Kind.RELEASE_GROUP, group.mbid)
            selection = _pick(
                [
                    f"{group.title} | {group.primary_type or '?'} | "
                    f"{(group.first_release_date or '?')[:4]} | {artist} | {score}"
                    for group, score, artist in albums[:8]
                ]
            )
            return (
                None if selection is None else MbLink(Kind.RELEASE_GROUP, albums[selection][0].mbid)
            )
        artists = await mb.search_artists(target.name)
        if not artists:
            _fail("No MusicBrainz artists found", 1)
        artist = artists[0]
        if yes or (
            (artist.score or 0) >= 90 and norm_artist(artist.name) == norm_artist(target.name)
        ):
            return MbLink(Kind.ARTIST, artist.mbid)
        selection = _pick(
            [
                f"{artist.name} | {artist.sort_name} | {artist.score} | {artist.mbid}"
                for artist in artists[:8]
            ]
        )
        return None if selection is None else MbLink(Kind.ARTIST, artists[selection].mbid)


_DEFAULT_SCOPE = {
    Kind.ARTIST: Scope.DISCOGRAPHY,
    Kind.RELEASE_GROUP: Scope.ALBUM,
    Kind.RELEASE: Scope.ALBUM,
    Kind.RECORDING: Scope.TRACK,
}


@app.command()
def add(
    ctx: typer.Context,
    target: str,
    scope: Annotated[Scope | None, typer.Option("--scope")] = None,
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Accept the first search result")] = False,
) -> None:
    """Add a MusicBrainz link, artist, or artist - album to the wants list."""
    try:
        parsed = parse_add_target(target)
    except ValueError as exc:
        _fail(str(exc), 2)
    if isinstance(parsed, ForeignLink):
        _fail(
            f"links from {parsed.service} are supported from step 5; "
            "use a MusicBrainz link or text",
            2,
        )
    if limit is not None and (
        isinstance(parsed, TextAlbum) or (isinstance(parsed, MbLink) and parsed.kind != Kind.ARTIST)
    ):
        _fail("--limit applies only to artist wants", 2)
    with _session(ctx.obj) as (cfg, conn):
        link = (
            parsed if isinstance(parsed, MbLink) else asyncio.run(_resolve(cfg, conn, parsed, yes))
        )
        if link is None:
            typer.echo("Cancelled")
            return
        if link.kind == Kind.ARTIST and limit is None:
            limit = cfg.plan.discography_limit
        with conn:
            want_id = create_want(
                conn,
                link.kind,
                link.mbid,
                scope or _DEFAULT_SCOPE[link.kind],
                Origin.MANUAL,
                target,
                limit,
            )
        typer.echo(f"Want {want_id}: {link.kind} {link.mbid}")


@app.command()
def plan(ctx: typer.Context, refresh: Annotated[bool, typer.Option("--refresh")] = False) -> None:
    """Turn new wants into planned releases and tracks."""
    with _session(ctx.obj) as (cfg, conn):

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


@app.command()
def status(ctx: typer.Context) -> None:
    """Show track and want counts."""
    with _session(ctx.obj) as (_, conn):
        counts = repo.status(conn)
    console = Console()
    tracks = Table("State", "Count", title="Tracks")
    for state, count in counts.tracks:
        tracks.add_row(state, str(count))
    wants = Table("Status", "Origin", "Count", title="Wants")
    for state, origin, count in counts.wants:
        wants.add_row(state, origin, str(count))
    console.print(tracks, wants, sep="\n")
    typer.echo(f"Unresolved: {counts.unresolved}")


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


@app.command()
def explain(ctx: typer.Context, target: str) -> None:
    """Explain which wants, release decisions and attempts led to a track or album."""
    try:
        kind, value = _explain_target(target)
    except ValueError as exc:
        _fail(str(exc), 2)
    with _session(ctx.obj) as (_, conn):
        explanation = repo.explain(conn, kind, value)
    if not explanation.releases:
        _fail(f"No planned tracks match {target}", 1)
    console = Console()
    for release in explanation.releases:
        typer.echo(f"Release: {release.title} ({release.mbid})\nchoice_reason:")
        console.print_json(release.choice_reason)
    typer.echo("Wants:")
    for want in explanation.wants:
        typer.echo(
            f"  #{want.id} {want.kind} | {want.origin} | {want.origin_ref} | {want.created_at}"
        )
    typer.echo("Attempts:")
    if not explanation.attempts:
        typer.echo("  None")
    for attempt in explanation.attempts:
        typer.echo(
            f"  Track {attempt.track_id} | {attempt.at} | {attempt.source} | {attempt.outcome}"
            f" | {attempt.candidate_ref or '-'} | score: {attempt.score} | {attempt.reason or '-'}"
        )


@app.command("doctor")
def doctor_command(
    ctx: typer.Context, offline: Annotated[bool, typer.Option("--offline")] = False
) -> None:
    """Check configuration, directories, schema, tools, and MusicBrainz."""
    opts: Options = ctx.obj
    checks = asyncio.run(doctor.check(opts.config, offline))
    table = Table("Check", "Status", "Detail")
    for result in checks:
        table.add_row(result.name, result.status, Text(result.detail))
    Console().print(table)
    if any(result.status == "FAIL" for result in checks):
        raise typer.Exit(1)
