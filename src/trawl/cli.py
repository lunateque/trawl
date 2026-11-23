import asyncio
import sqlite3
from collections.abc import AsyncGenerator, Generator, Sequence
from contextlib import asynccontextmanager, closing, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Never
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from trawl import __version__, doctor, log
from trawl.config import Config, ConfigError, Secrets, config_path, load, load_secrets
from trawl.db import queue, repo
from trawl.db.connect import open_db
from trawl.db.repo import Kind, Origin, Scope
from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import MbDataError
from trawl.net.client import HttpClient, HttpError
from trawl.net.limiter import RateLimiter
from trawl.normalize import norm_artist, norm_title
from trawl.plan.import_folder import plan_import
from trawl.plan.run import plan_new_wants
from trawl.plan.wants import (
    ForeignLink,
    MbLink,
    TextAlbum,
    TextArtist,
    create_want,
    parse_add_target,
)
from trawl.resolve.match import resolve_artist
from trawl.runner.lock import LockBusy, acquire
from trawl.runner.run import RunDeps, run_once
from trawl.runner.service import service as manage_service
from trawl.runner.upgrade import upgrade_pass
from trawl.runner.watch import Job, graceful_stop
from trawl.runner.watch import watch as watch_jobs
from trawl.sources.base import Source
from trawl.sources.local import LocalSource
from trawl.sources.soulseek import SoulseekSource
from trawl.sources.ytmusic import YTMusic, YTMusicSource, video_id
from trawl.taste.links import entity, metadata
from trawl.taste.oauth import auth_spotify
from trawl.taste.releases import save_artist
from trawl.taste.store import DEFAULT_SCOPE, SyncReport, bind, ingest
from trawl.taste.sync import ERRORS as TASTE_ERRORS
from trawl.taste.sync import TasteSource, read_secrets, sync_taste, taste_http
from trawl.verify.fingerprint import AcoustId
from trawl.verify.probe import AudioError

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


@app.command()
def add(
    ctx: typer.Context,
    target: str,
    scope: Annotated[Scope | None, typer.Option("--scope")] = None,
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Accept the first search result")] = False,
) -> None:
    """Add a music link, artist, or artist - album to the wants list."""
    try:
        parsed = parse_add_target(target)
    except ValueError as exc:
        _fail(str(exc), 2)
    if isinstance(parsed, ForeignLink):
        try:
            kind, _ = entity(parsed)
        except ConfigError as exc:
            _fail(str(exc), 2)
        if limit is not None and kind != "artist":
            _fail("--limit applies only to artist wants", 2)
        with _session(ctx.obj) as (cfg, conn):
            asyncio.run(_add_foreign(cfg, conn, parsed, scope, limit))
        return
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
                scope or DEFAULT_SCOPE[link.kind],
                Origin.MANUAL,
                target,
                limit,
            )
        typer.echo(f"Want {want_id}: {link.kind} {link.mbid}")


async def _add_foreign(
    cfg: Config, conn: sqlite3.Connection, link: ForeignLink, scope: Scope | None, limit: int | None
) -> None:
    report = SyncReport()
    try:
        async with taste_http(cfg) as http, make_mb(cfg, conn) as mb:
            tracks = await metadata(http, read_secrets(cfg), link)
            if not tracks:
                _fail(f"{link.service} did not return any media", 1)
            for track in tracks:
                await ingest(
                    conn,
                    mb,
                    cfg,
                    track,
                    report,
                    scope=scope or DEFAULT_SCOPE[track.kind],
                    limit=limit,
                )
    except TASTE_ERRORS as exc:
        _fail(str(exc), 1)
    typer.echo(f"Wants: {report.wants}; unresolved: {report.unresolved}")


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


def _source_report(
    console: Console,
    outcomes: list[tuple[str, int, int]],
    pauses: list[queue.Pause],
) -> None:
    summary = Table("Source", "Accepted", "Rejected", title="Attempts")
    for source, accepted, rejected in outcomes:
        summary.add_row(source, str(accepted), str(rejected))
    console.print(summary)
    for pause in pauses:
        console.print(Text(f"Wall: {pause.source} until {pause.until} | {pause.reason}"))


@asynccontextmanager
async def _run_deps(
    cfg: Config, conn: sqlite3.Connection
) -> AsyncGenerator[tuple[Secrets, RunDeps]]:
    if cfg.source_path is None:
        raise ConfigError("Processing requires a configuration file")
    secrets = load_secrets(cfg.source_path)
    if not (key := secrets.acoustid_key):
        _fail("Configure acoustid_key in secrets.toml (https://acoustid.org/new-application)", 1)
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


async def _process(
    cfg: Config,
    conn: sqlite3.Connection,
    only: int | None = None,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    async with _run_deps(cfg, conn) as (secrets, deps):
        report = await run_once(conn, cfg, secrets, deps, only=only, stop=stop)
    _show_run(report.states, report.outcomes, report.pauses)


def _show_run(
    states: list[tuple[str, int]], outcomes: list[tuple[str, int, int]], pauses: list[queue.Pause]
) -> None:
    console = Console()
    table = Table("State", "Count", title="Tracks")
    for state, count in states:
        table.add_row(state, str(count))
    console.print(table)
    _source_report(console, outcomes, pauses)


@app.command()
def run(
    ctx: typer.Context,
    *,
    once: Annotated[bool, typer.Option("--once", help="Process the current queue once")] = False,
    watch: Annotated[bool, typer.Option("--watch", help="Process the queue on a schedule")] = False,
) -> None:
    """Process the current queue."""
    if once and watch:
        _fail("Choose either --once or --watch", 2)
    with _session(ctx.obj) as (cfg, conn):
        try:
            with acquire(cfg.data_dir):
                asyncio.run(_watch(cfg, conn) if watch else _process(cfg, conn))
        except LockBusy as exc:
            _fail(str(exc), 3)


async def _watch(cfg: Config, conn: sqlite3.Connection) -> None:
    stop = asyncio.Event()

    async def taste(source: TasteSource) -> None:
        async with make_mb(cfg, conn) as mb:
            await _sync(conn, mb, cfg, source, releases=False)

    async def planning() -> None:
        async with make_mb(cfg, conn) as mb:
            report = await plan_new_wants(conn, mb, cfg.plan)
        if report.errors:
            raise ConfigError(f"{report.errors} wants failed to plan")

    async def retry_due() -> None:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            queue.retry(conn, skipped=False, due=queue.now())

    with graceful_stop(stop):
        await watch_jobs(
            conn,
            [
                Job("taste", cfg.watch.taste_every, lambda: taste(TasteSource.ALL)),
                Job("releases", cfg.watch.releases_every, lambda: taste(TasteSource.RELEASES)),
                Job("plan", cfg.watch.queue_every, planning),
                Job("run", cfg.watch.queue_every, lambda: _process(cfg, conn, stop=stop)),
                Job("retry", cfg.watch.queue_every, retry_due),
                Job(
                    "upgrade",
                    cfg.watch.upgrade_every,
                    lambda: _upgrade(cfg, conn, False, stop=stop),
                ),
            ],
            stop,
        )


async def _import_folder(cfg: Config, conn: sqlite3.Connection, directory: Path) -> None:
    async with _run_deps(cfg, conn) as (secrets, deps):
        release = await plan_import(conn, cfg, deps.mb, deps.acoustid, directory)
        typer.echo(f"Import: {release.title} ({release.mbid})")
        report = await run_once(conn, cfg, secrets, deps, release_mbid=release.mbid)
    _show_run(report.states, report.outcomes, report.pauses)


@app.command("import")
def import_command(
    ctx: typer.Context,
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
) -> None:
    """Identify an album folder by fingerprint and import its files."""
    with _session(ctx.obj) as (cfg, conn):
        try:
            with acquire(cfg.data_dir):
                asyncio.run(_import_folder(cfg, conn, directory))
        except LockBusy as exc:
            _fail(str(exc), 3)
        except (AudioError, ValueError) as exc:
            _fail(str(exc), 1)


async def _upgrade(
    cfg: Config, conn: sqlite3.Connection, now: bool, *, stop: asyncio.Event | None = None
) -> None:
    async with _run_deps(cfg, conn) as (_, deps):
        report = await upgrade_pass(conn, cfg, deps, now=now, stop=stop)
    typer.echo(
        f"Releases checked: {report.checked}; "
        f"tracks upgraded: {report.replaced}; deferred: {report.deferred}"
    )


@app.command()
def upgrade(ctx: typer.Context, now: Annotated[bool, typer.Option("--now")] = False) -> None:
    """Replace verified lossy library audio with lossless files."""
    with _session(ctx.obj) as (cfg, conn):
        try:
            with acquire(cfg.data_dir):
                asyncio.run(_upgrade(cfg, conn, now))
        except LockBusy as exc:
            _fail(str(exc), 3)


@app.command()
def retry(ctx: typer.Context, skipped: Annotated[bool, typer.Option("--skipped")] = False) -> None:
    """Return missing tracks (and optionally skipped tracks) to the queue."""
    with _session(ctx.obj) as (_, conn), conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = queue.retry(conn, skipped=skipped)
    typer.echo(f"Requeued: {changed}")


@app.command()
def pick(ctx: typer.Context, track: int, candidate: str) -> None:
    """Verify a particular YouTube video or local file for a track."""
    if "://" in candidate:
        try:
            ref, source = video_id(candidate), "ytmusic"
        except ValueError as exc:
            _fail(str(exc), 2)
    else:
        path = Path(candidate).resolve()
        if not path.is_file():
            _fail(f"No audio file at {path}", 2)
        ref, source = str(path), "path"
    with _session(ctx.obj) as (cfg, conn):
        if cfg.source_path is None:
            raise ConfigError("Pick requires a configuration file")
        if not load_secrets(cfg.source_path).acoustid_key:
            _fail("AcoustID key required for pick; configure acoustid_key in secrets.toml", 1)
        if source == "ytmusic" and source not in cfg.sources.order:
            _fail("Enable ytmusic in sources.order to pick a YouTube video", 1)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if not queue.pin(conn, track, source, ref):
                _fail(f"Track {track} cannot be picked in its current state", 1)
        try:
            with acquire(cfg.data_dir):
                asyncio.run(_process(cfg, conn, track))
        except LockBusy as exc:
            typer.echo(f"Pinned track {track}; {exc}")
            return
        row = conn.execute("SELECT state FROM tracks WHERE id=?", (track,)).fetchone()
        if row[0] == "missing":
            attempt = conn.execute(
                "SELECT reason FROM attempts WHERE track_id=? ORDER BY id DESC LIMIT 1", (track,)
            ).fetchone()
            _fail(f"Track {track} missing: {attempt[0] if attempt else 'candidate rejected'}", 1)


@app.command()
def skip(ctx: typer.Context, target: str) -> None:
    """Skip an unfinished track or the unfinished tracks of a release."""
    try:
        kind, key = _explain_target(target)
        if kind not in {None, Kind.RELEASE}:
            raise ValueError("expected a track ID or release MBID")
    except ValueError as exc:
        _fail(str(exc), 2)
    with _session(ctx.obj) as (_, conn), conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = queue.skip(conn, key)
    typer.echo(f"Skipped: {changed}")


@app.command()
def status(ctx: typer.Context) -> None:
    """Show track and want counts."""
    with _session(ctx.obj) as (_, conn):
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
    _source_report(console, outcomes, pauses)


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


async def _sync(
    conn: sqlite3.Connection,
    mb: MusicBrainzClient,
    cfg: Config,
    source: TasteSource,
    *,
    releases: bool = True,
) -> None:
    results = await sync_taste(conn, mb, cfg, source, releases=releases)
    for name, report, _ in results:
        detail = f"; {report.message}" if report.message else ""
        typer.echo(f"{name}: {report.wants} wants; {report.unresolved} unresolved{detail}")
    if any(not ok for _, _, ok in results):
        raise typer.Exit(1)


@app.command("sync-taste")
def sync_taste_command(ctx: typer.Context, source: TasteSource = TasteSource.ALL) -> None:
    """Sync listening history, likes, playlists, or followed releases."""
    with _session(ctx.obj) as (cfg, conn):

        async def execute() -> None:
            async with make_mb(cfg, conn) as mb:
                await _sync(conn, mb, cfg, source)

        asyncio.run(execute())


@app.command()
def link(ctx: typer.Context, unresolved_id: int, target: str) -> None:
    """Resolve an external item with a MusicBrainz link."""
    try:
        parsed = parse_add_target(target)
    except ValueError as exc:
        _fail(str(exc), 2)
    if not isinstance(parsed, MbLink):
        _fail("Expected a MusicBrainz link", 2)
    with _session(ctx.obj) as (cfg, conn):
        want_id = bind(conn, unresolved_id, parsed, cfg.plan.discography_limit)
    if want_id is None:
        _fail(f"Unresolved item {unresolved_id} does not exist", 1)
    typer.echo(f"Want {want_id}: {parsed.kind} {parsed.mbid}")


@app.command()
def unresolved(ctx: typer.Context) -> None:
    """List external items not matched to MusicBrainz."""
    with _session(ctx.obj) as (_, conn):
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
        _fail(str(exc), 2)
    if not (
        isinstance(parsed, TextArtist)
        or (isinstance(parsed, MbLink) and parsed.kind == Kind.ARTIST)
    ):
        _fail("Expected a MusicBrainz artist link or name", 2)
    with _session(ctx.obj) as (cfg, conn):

        async def execute() -> None:
            async with make_mb(cfg, conn) as mb:
                artist = (
                    await mb.artist(parsed.mbid)
                    if isinstance(parsed, MbLink)
                    else await resolve_artist(mb, parsed.name)
                )
            if artist is None:
                _fail("No exact MusicBrainz artist found; use an artist link", 1)
            with conn:
                save_artist(conn, artist, followed=followed, manual=True)
            typer.echo(f"{'Following' if followed else 'Unfollowed'}: {artist.name}")

        asyncio.run(execute())


@app.command()
def follow(ctx: typer.Context, artist: str) -> None:
    """Follow new releases by a MusicBrainz artist."""
    _follow(ctx, artist, True)


@app.command()
def unfollow(ctx: typer.Context, artist: str) -> None:
    """Stop following an artist's new releases."""
    _follow(ctx, artist, False)


class AuthProvider(StrEnum):
    SPOTIFY = "spotify"


@app.command()
def auth(ctx: typer.Context, provider: AuthProvider) -> None:
    """Authorize a taste provider in your browser."""
    opts: Options = ctx.obj
    with _session(opts) as (cfg, _):
        if not (client_id := read_secrets(cfg).spotify_client_id):
            _fail("Configure spotify_client_id in secrets.toml first", 1)

        async def execute() -> None:
            async with taste_http(cfg) as http:
                await auth_spotify(http, client_id, opts.config.with_name("secrets.toml"))

        asyncio.run(execute())
    typer.echo(f"Authorized {provider}")


class ServiceAction(StrEnum):
    INSTALL = "install"
    UNINSTALL = "uninstall"
    STATUS = "status"


@app.command()
def service(ctx: typer.Context, action: ServiceAction) -> None:
    """Manage the background watch service."""
    opts: Options = ctx.obj
    try:
        if action == ServiceAction.INSTALL:
            load(opts.config)
        code = manage_service(action, opts.config)
    except (ConfigError, OSError) as exc:
        _fail(str(exc), 1)
    if code:
        raise typer.Exit(code)
