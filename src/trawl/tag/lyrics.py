import re
from dataclasses import dataclass

from trawl.mb.models import object_value
from trawl.net.client import HttpClient, HttpError

_TIMESTAMP = re.compile(r"\[\d+:\d+(?:\.\d+)?\]")


@dataclass(frozen=True)
class Lyrics:
    synced: str | None
    plain: str | None

    @property
    def embedded(self) -> str | None:
        if self.plain:
            return self.plain
        if self.synced:
            return "\n".join(_TIMESTAMP.sub("", line).strip() for line in self.synced.splitlines())
        return None


async def fetch_lyrics(
    http: HttpClient,
    artist: str,
    title: str,
    album: str,
    duration_s: int,
) -> Lyrics | None:
    try:
        body = object_value(
            await http.get_json(
                "https://lrclib.net/api/get",
                {
                    "artist_name": artist,
                    "track_name": title,
                    "album_name": album,
                    "duration": str(duration_s),
                },
                limit=2_000_000,
            )
        )
    except HttpError as exc:
        if exc.status != 404:
            raise
        return None
    synced, plain = body.get("syncedLyrics"), body.get("plainLyrics")
    return Lyrics(
        synced if isinstance(synced, str) else None, plain if isinstance(plain, str) else None
    )
