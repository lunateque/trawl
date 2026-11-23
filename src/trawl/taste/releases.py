import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta

from trawl.db.repo import Kind, Origin, Scope, insert_want
from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import ArtistRef, MbDataError
from trawl.net.client import HttpError
from trawl.taste.store import SyncReport, moment

_LOG = logging.getLogger(__name__)


def save_artist(
    conn: sqlite3.Connection, artist: ArtistRef, *, followed: bool, manual: bool = False
) -> None:
    conn.execute(
        """INSERT INTO artists(mbid,name,sort_name,followed,follow_override) VALUES (?,?,?,?,?)
        ON CONFLICT(mbid) DO UPDATE SET name=excluded.name,sort_name=excluded.sort_name,
        followed=CASE WHEN excluded.follow_override IS NOT NULL THEN excluded.followed
            ELSE coalesce(artists.follow_override,max(artists.followed,excluded.followed)) END,
        follow_override=coalesce(excluded.follow_override,artists.follow_override)""",
        (
            artist.mbid,
            artist.name,
            artist.sort_name,
            int(followed),
            int(followed) if manual else None,
        ),
    )


def release_date(value: str | None) -> date | None:
    if not value:
        return None
    if len(value) == 4:
        value += "-01"
    if len(value) == 7:
        value += "-01"
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


async def sync_releases(
    conn: sqlite3.Connection,
    mb: MusicBrainzClient,
    types: tuple[str, ...],
    *,
    now: datetime | None = None,
) -> SyncReport:
    checked = now if now is not None else datetime.now(UTC)
    report = SyncReport()
    failures = 0
    kinds = {kind.casefold() for kind in types}
    artists = conn.execute(
        "SELECT mbid,releases_checked_at FROM artists WHERE followed=1 ORDER BY mbid"
    ).fetchall()
    for artist, last in artists:
        edge = moment(last).date() if last else (checked - timedelta(days=90)).date()
        previous = mb.refresh
        mb.refresh = True
        try:
            groups = await mb.artist_release_groups(artist, types)
        except (HttpError, MbDataError) as exc:
            failures += 1
            _LOG.warning(
                "taste.releases_failed", extra={"fields": {"artist": artist, "error": str(exc)}}
            )
            continue
        finally:
            mb.refresh = previous
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for group in groups:
                if (group.primary_type or "").casefold() not in kinds:
                    continue
                if (released := release_date(group.first_release_date)) and (
                    edge < released <= checked.date()
                ):
                    before = conn.total_changes
                    insert_want(
                        conn,
                        Kind.RELEASE_GROUP,
                        group.mbid,
                        Scope.ALBUM,
                        Origin.RELEASES,
                        f"artist:{artist}",
                        None,
                        preserve_origin=True,
                    )
                    report.wants += conn.total_changes != before
            conn.execute(
                "UPDATE artists SET releases_checked_at=? WHERE mbid=? AND followed=1",
                (checked.isoformat(), artist),
            )
    if failures:
        report.message = f"Failed to check {failures} followed artist(s)"
    return report
