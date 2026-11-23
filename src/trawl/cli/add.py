import asyncio
import sqlite3
from typing import Annotated

import typer

from trawl.cli.context import fail, make_mb, select_candidate, session
from trawl.config.models import Config, ConfigError
from trawl.core.normalize import norm_artist, norm_title
from trawl.db.repo import Kind, Origin, Scope
from trawl.plan.wants import (
    ForeignLink,
    MbLink,
    TextAlbum,
    TextArtist,
    create_want,
    parse_add_target,
)
from trawl.taste.links import entity, metadata
from trawl.taste.store import DEFAULT_SCOPE, SyncReport, ingest
from trawl.taste.sync import ERRORS as TASTE_ERRORS
from trawl.taste.sync import read_secrets, taste_http


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
                fail("No MusicBrainz release groups found", 1)
            group, score, artist = albums[0]
            if yes or (
                score >= 90
                and norm_artist(artist) == norm_artist(target.artist)
                and norm_title(group.title) == norm_title(target.title)
            ):
                return MbLink(Kind.RELEASE_GROUP, group.mbid)
            selection = select_candidate(
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
            fail("No MusicBrainz artists found", 1)
        artist = artists[0]
        if yes or (
            (artist.score or 0) >= 90 and norm_artist(artist.name) == norm_artist(target.name)
        ):
            return MbLink(Kind.ARTIST, artist.mbid)
        selection = select_candidate(
            [
                f"{artist.name} | {artist.sort_name} | {artist.score} | {artist.mbid}"
                for artist in artists[:8]
            ]
        )
        return None if selection is None else MbLink(Kind.ARTIST, artists[selection].mbid)


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
        fail(str(exc), 2)
    if isinstance(parsed, ForeignLink):
        try:
            kind, _ = entity(parsed)
        except ConfigError as exc:
            fail(str(exc), 2)
        if limit is not None and kind != "artist":
            fail("--limit applies only to artist wants", 2)
        with session(ctx.obj) as (cfg, conn):
            asyncio.run(_add_foreign(cfg, conn, parsed, scope, limit))
        return
    if limit is not None and (
        isinstance(parsed, TextAlbum) or (isinstance(parsed, MbLink) and parsed.kind != Kind.ARTIST)
    ):
        fail("--limit applies only to artist wants", 2)
    with session(ctx.obj) as (cfg, conn):
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
                fail(f"{link.service} did not return any media", 1)
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
        fail(str(exc), 1)
    typer.echo(f"Wants: {report.wants}; unresolved: {report.unresolved}")
