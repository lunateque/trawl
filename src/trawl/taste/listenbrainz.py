import sqlite3
from collections import Counter
from collections.abc import AsyncIterator
from urllib.parse import quote

from trawl.config.models import Config, ListenBrainzConfig, TasteTier
from trawl.core.catalog import ArtistRef, JsonObject, array_value, object_value
from trawl.core.normalize import norm_artist
from trawl.db.repo import Kind, Origin, Scope, insert_want
from trawl.mb.client import MusicBrainzClient
from trawl.net.client import HttpClient, HttpError
from trawl.plan.choose import choose_recording_group
from trawl.resolve.match import ExternalTrack, resolve_artist
from trawl.taste.releases import save_artist
from trawl.taste.store import SyncReport, ingest, unresolved_tracks

BASE = "https://api.listenbrainz.org/1"


def text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class ListenBrainz:
    def __init__(self, http: HttpClient, cfg: ListenBrainzConfig, token: str | None) -> None:
        self.http = http
        self.cfg = cfg
        self.user = quote(cfg.user, safe="")
        self.headers = {"Authorization": f"Token {token}"} if token else {}
        self.not_computed = False

    async def check(self) -> None:
        await self.http.api_json(
            "GET", f"{BASE}/user/{self.user}/listen-count", headers=self.headers
        )

    async def stats(self, category: str) -> AsyncIterator[JsonObject]:
        offset = 0
        while True:
            raw = await self.http.api_json(
                "GET",
                f"{BASE}/stats/user/{self.user}/{category}",
                headers=self.headers,
                params={"range": self.cfg.range, "count": "100", "offset": str(offset)},
            )
            if raw is None:
                self.not_computed = True
                return
            payload = object_value(object_value(raw).get("payload"))
            rows = array_value(payload.get(category))
            for row in rows:
                yield object_value(row)
            offset += len(rows)
            total = number(payload.get(f"total_{category.removesuffix('s')}_count"))
            if (
                not rows
                or (total is not None and offset >= total)
                or (total is None and len(rows) < 100)
            ):
                return


def tier_for(plays: int, tiers: tuple[TasteTier, ...]) -> tuple[int, TasteTier] | None:
    return next(
        ((rank, tier) for rank, tier in enumerate(tiers, 1) if plays >= tier.min_plays), None
    )


type _Artists = dict[str, tuple[ArtistRef, int, TasteTier]]


async def _artists(artist_rows: list[JsonObject], mb: MusicBrainzClient, cfg: Config) -> _Artists:
    artists: dict[str, tuple[ArtistRef, int, TasteTier]] = {}
    for row in artist_rows:
        name = text(row.get("artist_name"))
        if not name or not (
            ranked := tier_for(number(row.get("listen_count")) or 0, cfg.taste.tiers)
        ):
            continue
        if mbid := text(row.get("artist_mbid")):
            artist = ArtistRef(mbid, name, name, None)
        else:
            artist = await resolve_artist(mb, name)
        if artist is not None:
            artists[artist.mbid] = (artist, *ranked)
    return artists


async def _group(row: JsonObject, recording: str | None, mb: MusicBrainzClient) -> str | None:
    group_mbid = text(row.get("release_group_mbid"))
    if not group_mbid and (release := text(row.get("release_mbid"))):
        try:
            group_mbid = (await mb.release(release)).release_group.mbid
        except HttpError as exc:
            if exc.status != 404:
                raise
    if not group_mbid and recording:
        group = choose_recording_group(await mb.recording_releases(recording))
        group_mbid = group.mbid if group else None
    return group_mbid


async def _recordings(
    conn: sqlite3.Connection,
    mb: MusicBrainzClient,
    cfg: Config,
    recordings: list[JsonObject],
    artists: _Artists,
    min_track_plays: int,
    report: SyncReport,
) -> tuple[Counter[tuple[str, str]], set[str]]:
    by_name = {norm_artist(artist.name): artist.mbid for artist, _, _ in artists.values()}
    plays: Counter[tuple[str, str]] = Counter()
    seen: set[str] = set()
    for row in recordings:
        count = number(row.get("listen_count")) or 0
        if count <= 0:
            continue
        name = text(row.get("artist_name")) or ""
        title = text(row.get("track_name")) or ""
        album = text(row.get("release_name"))
        recording = text(row.get("recording_mbid"))
        artist_ids = {
            identifier
            for value in array_value(row.get("artist_mbids"))
            if (identifier := text(value)) is not None and identifier in artists
        }
        if not artist_ids and (artist_id := by_name.get(norm_artist(name))):
            artist_ids.add(artist_id)
        if artist_ids:
            group_mbid = await _group(row, recording, mb)
            if group_mbid:
                for artist_id in artist_ids:
                    plays[artist_id, group_mbid] += count
        if count >= min_track_plays and (recording or (name and title)):
            ref = f"recording:{recording}" if recording else f"track:{name}:{title}:{album or ''}"
            seen.add(ref)
            await ingest(
                conn,
                mb,
                cfg,
                ExternalTrack(Origin.LISTENBRAINZ, ref, name, title, album),
                report,
                known_mbid=recording,
            )
    return plays, seen


def _store_taste(
    conn: sqlite3.Connection,
    cfg: Config,
    artists: _Artists,
    plays: Counter[tuple[str, str]],
    not_computed: bool,
    report: SyncReport,
) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if not not_computed:
            conn.execute("DELETE FROM artist_release_plays")
            conn.executemany(
                "INSERT INTO artist_release_plays VALUES (?,?,?)",
                ((artist, group, count) for (artist, group), count in plays.items()),
            )
        for artist, rank, tier in artists.values():
            save_artist(conn, artist, followed=rank <= cfg.taste.follow_max_tier)
            ref = f"artist:{artist.mbid}"
            row = conn.execute(
                """SELECT id FROM wants WHERE origin=? AND origin_ref=? AND status!='dropped'""",
                (Origin.LISTENBRAINZ, ref),
            ).fetchone()
            if row is None:
                insert_want(
                    conn,
                    Kind.ARTIST,
                    artist.mbid,
                    Scope(tier.scope),
                    Origin.LISTENBRAINZ,
                    ref,
                    tier.limit,
                    preserve_origin=True,
                )
                report.wants += 1
            else:
                conn.execute(
                    """UPDATE wants SET "limit"=?,scope=?,status='new'
                    WHERE id=? AND coalesce("limit",0) < ?""",
                    (tier.limit, tier.scope, row[0], tier.limit),
                )


async def sync_listenbrainz(
    conn: sqlite3.Connection, mb: MusicBrainzClient, cfg: Config, client: ListenBrainz
) -> SyncReport:
    artist_rows = [row async for row in client.stats("artists")]
    recordings = [row async for row in client.stats("recordings")]
    report = SyncReport(message="Stats not computed yet" if client.not_computed else "")
    artists = await _artists(artist_rows, mb, cfg)
    plays, seen = await _recordings(
        conn, mb, cfg, recordings, artists, client.cfg.min_track_plays, report
    )
    if not client.not_computed:
        for track in unresolved_tracks(conn, Origin.LISTENBRAINZ):
            if track.origin_ref not in seen:
                await ingest(conn, mb, cfg, track, report)
    _store_taste(conn, cfg, artists, plays, client.not_computed, report)
    return report
