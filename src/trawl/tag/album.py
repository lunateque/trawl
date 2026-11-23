import asyncio
from pathlib import Path

from trawl.config.models import Config
from trawl.core.audio import AudioTools
from trawl.core.catalog import Release, Track
from trawl.core.paths import write_atomic
from trawl.core.tags import TagFields
from trawl.mb.client import MusicBrainzClient
from trawl.net.client import HttpClient
from trawl.tag.cover import Cover, fetch_cover
from trawl.tag.lyrics import fetch_lyrics
from trawl.tag.mapping import fields_for


class AlbumTags:
    def __init__(
        self,
        cfg: Config,
        http: HttpClient,
        mb: MusicBrainzClient,
        tools: AudioTools,
        release: Release,
    ) -> None:
        self.cfg, self.http, self.mb, self.tools, self.release = cfg, http, mb, tools, release
        self.cover: Cover | None = None
        self.cover_loaded = False
        self.genres: tuple[str, ...] | None = None

    async def write(self, path: Path, track: Track) -> TagFields:
        release = self.release
        if not self.cover_loaded:
            self.cover = await fetch_cover(self.http, release.mbid, release.release_group.mbid)
            self.cover_loaded = True
        if self.genres is None:
            self.genres = (
                await self.mb.artist_genres(release.credits[0].artist_mbid)
                if not release.genres and release.credits
                else ()
            )
        fields = fields_for(release, track, self.cfg.genres, self.genres)
        lyrics = None
        if self.cfg.lyrics_enabled:
            duration = track.length_ms
            if duration is None:
                duration = (await asyncio.to_thread(self.tools.probe, path)).duration_ms
            lyrics = await fetch_lyrics(
                self.http, fields.artist, fields.title, fields.album, round(duration / 1000)
            )
        await asyncio.to_thread(
            self.tools.write,
            path,
            fields,
            self.cover.embed if self.cover else None,
            lyrics.embedded if lyrics else None,
        )
        if lyrics and lyrics.synced:
            await asyncio.to_thread(write_atomic, path.with_suffix(".lrc"), lyrics.synced.encode())
        if self.cover:
            await asyncio.to_thread(write_atomic, path.parent / "cover.jpg", self.cover.full)
        await asyncio.to_thread(self.tools.track_gain, path)
        return fields
