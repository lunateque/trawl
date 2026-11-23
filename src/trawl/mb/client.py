import json
import sqlite3
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime

import httpx

from trawl.mb.models import (
    ArtistRef,
    Credit,
    JsonObject,
    RecordingRef,
    Release,
    ReleaseGroupRef,
    ReleaseRef,
    array_value,
    credit_string,
    object_value,
)
from trawl.net.client import HttpClient


def _phrase(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class MusicBrainzClient:
    BASE = "https://musicbrainz.org/ws/2"

    def __init__(
        self,
        http: HttpClient,
        conn: sqlite3.Connection,
        cache_days: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._http = http
        self._conn = conn
        self._cache_days = cache_days
        self._now = now
        self.refresh = False

    async def _get(self, endpoint: str, **params: str) -> JsonObject:
        url = f"{self.BASE}/{endpoint}"
        query = dict(sorted({"fmt": "json", **params}.items()))
        key = str(httpx.URL(url, params=query))
        if not self.refresh and self._cache_days:
            cached = self._conn.execute(
                "SELECT body, fetched_at FROM mb_cache WHERE url = ?", (key,)
            ).fetchone()
            if cached is not None:
                age = self._now() - datetime.fromisoformat(cached["fetched_at"])
                if age.total_seconds() < self._cache_days * 86_400:
                    return object_value(json.loads(cached["body"]))
        body = object_value(await self._http.get_json(url, query))
        with self._conn:
            self._conn.execute(
                """INSERT INTO mb_cache (url, body, fetched_at) VALUES (?, ?, ?)
                ON CONFLICT (url) DO UPDATE
                SET body = excluded.body, fetched_at = excluded.fetched_at""",
                (key, json.dumps(body, ensure_ascii=False), self._now().isoformat()),
            )
        return body

    async def _browse(self, endpoint: str, **params: str) -> AsyncIterator[JsonObject]:
        offset = 0
        while True:
            data = await self._get(endpoint, limit="100", offset=str(offset), **params)
            items = array_value(data.get(f"{endpoint}s"))
            for item in items:
                yield object_value(item)
            offset += len(items)
            count = data.get(f"{endpoint}-count")
            if not items or not isinstance(count, int) or offset >= count:
                return

    async def release(self, mbid: str) -> Release:
        data = await self._get(
            f"release/{mbid}",
            inc="recordings+artist-credits+release-groups+media+isrcs+labels+genres",
        )
        return Release.from_json(data)

    async def release_group_releases(self, rg_mbid: str) -> list[Release]:
        return [
            Release.from_json(item)
            async for item in self._browse(
                "release",
                **{"release-group": rg_mbid},
                inc="media+recordings+artist-credits+release-groups",
            )
        ]

    async def recording(self, mbid: str) -> RecordingRef:
        data = await self._get(
            f"recording/{mbid}", inc="releases+release-groups+artist-credits+isrcs"
        )
        return RecordingRef.from_json(data)

    async def recording_releases(self, mbid: str) -> list[ReleaseRef]:
        return [
            ReleaseRef.from_json(item)
            async for item in self._browse("release", recording=mbid, inc="release-groups")
        ]

    async def artist_release_groups(
        self,
        artist_mbid: str,
        types: Sequence[str],
    ) -> list[ReleaseGroupRef]:
        if not types:
            return []
        return [
            ReleaseGroupRef.from_json(item)
            async for item in self._browse(
                "release-group", artist=artist_mbid, type="|".join(types)
            )
        ]

    async def search_artists(self, query: str) -> list[ArtistRef]:
        data = await self._get("artist", query=f"artist:{_phrase(query)}", limit="8")
        return [
            ArtistRef.from_json(object_value(item)) for item in array_value(data.get("artists"))
        ]

    async def search_release_groups(
        self,
        artist: str,
        title: str,
    ) -> list[tuple[ReleaseGroupRef, int, str]]:
        query = f"releasegroup:{_phrase(title)} AND artist:{_phrase(artist)}"
        data = await self._get("release-group", query=query, limit="8")
        results: list[tuple[ReleaseGroupRef, int, str]] = []
        for item in array_value(data.get("release-groups")):
            group = object_value(item)
            credits = array_value(group.get("artist-credit"))
            score = group.get("score")
            results.append(
                (
                    ReleaseGroupRef.from_json(group),
                    score if isinstance(score, int) else 0,
                    credit_string(Credit.from_json(object_value(credit)) for credit in credits),
                )
            )
        return results
