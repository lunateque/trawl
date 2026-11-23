# trawl step 2: first album end to end — implementation plan

**Goal:** `trawl run --once` takes planned tracks, finds them on YouTube Music, downloads the original audio stream, proves each file is the planned recording, tags it from MusicBrainz with cover, lyrics and ReplayGain, and places it atomically in the library.

**Spec:** `docs/specs/2026-11-23-trawl-design.md` (Russian) — sections Данные (transitions, reuse), Источники (protocol, score, ytmusic, wall), Проверка, Теги, Библиотека, Исполнение, Конфиг и секреты, Тестирование. The spec wins on conflicts; report them. Step 1 code is in `src/trawl` (read it first; reuse `HttpClient`, `RateLimiter`, `MusicBrainzClient`, `db.states.transition`, `repo`, `normalize`, `config`).

**Gate:** `uv run ruff check` · `uv run ruff format --check` · `uv run pyright` · `uv run pytest -q`

**New runtime deps:** `mutagen`, `yt-dlp`, `ytmusicapi`. External binaries become required: `ffmpeg`, `ffprobe`, `fpcalc`, `rsgain`.

---

## Rules

- No live network in tests. ytmusicapi responses are recorded once (script `tests/fixtures/record_ytm.py`) and replayed by patching the `YTMusic` methods; yt-dlp is behind a small `Downloader` protocol so tests use a fake that copies a generated file. AcoustID responses are fixtures shaped like the official docs (https://acoustid.org/webservice) — `{"status":"ok","results":[{"id":…,"score":…,"recordings":[{"id":…}]}]}` — including an empty result and a result without `recordings`.
- Audio fixtures are generated in tests with ffmpeg (`sine` source, 5–40 s, FLAC/Opus/M4A/MP3). Tests that need ffmpeg/fpcalc/rsgain are marked `@pytest.mark.tools` and skip with a clear reason when the binary is missing locally; CI installs them, so in CI nothing is skipped (add `-m` handling only if needed; never silently skip in CI: fail if `CI=true` and a tool is missing).
- Pure logic (score, policy, file↔track matching, tag mapping, path layout, safe names, wall detection) is tested table-style.
- Only the `ytmusic` source in this step. The source list in config is still a list; other names are rejected with "available from step 3/4".

## Config and secrets additions

`config.toml`:

```toml
[sources]
order = ["ytmusic"]

[sources.ytmusic]
concurrency = 2

[verify]
acoustid_min_score = 0.80

[library]
template = "{albumartist}/{original_year} - {album}{disambiguation}/{disc_prefix}{track:02} - {title}.{ext}"

[lyrics]
enabled = true

[run]
retry_missing_days = 7
```

`secrets.toml` (same dir as config, optional file, unknown keys are errors): `acoustid_key`, `youtube_cookies` (path, optional). `secrets.toml` path is next to the loaded config. Missing `acoustid_key` → `run` refuses to start with a message pointing at https://acoustid.org/new-application; `doctor` reports it.

## Components

### `net/client.py`
Add `post_form(url, data) -> Any` (same retry/limiter rules) and `get_bytes(url) -> bytes` that checks the body length against `Content-Length` when present (raise `HttpError` on mismatch). Limiters: `api.acoustid.org` 1/3 s, `lrclib.net` 0.3 s, `coverartarchive.org` 0.2 s.

### `runner/lock.py`
`acquire(data_dir) -> contextmanager` locking `trawl.lock` (`msvcrt.locking` / `fcntl.flock`, non-blocking), writing the PID; on failure raise `LockBusy(pid)`. `run` exits 3 with "another trawl is running (pid N)".

### `sources/base.py`
```python
@dataclass(frozen=True) class Candidate: source: str; ref: str; kind: Literal["album","track"]; title: str; codec: str | None; bitrate: int | None; lossless: bool; official: bool; items: tuple[CandidateItem, ...]; score: float = 0
@dataclass(frozen=True) class CandidateItem: ref: str; position: int | None; length_ms: int | None; title: str
@dataclass(frozen=True) class Fetched: item_ref: str; path: Path; codec: str; bitrate: int | None; lossless: bool
class Source(Protocol): name: str; async def search(release, tracks) -> list[Candidate]; async def fetch(candidate, into: Path) -> list[Fetched]
class SourceWall(Exception): reason: str
```
`fetch` may fetch only the items mapped to wanted tracks (album candidate with `scope=track` downloads one item).

### `sources/score.py` (pure)
- `match_items(tracks, items) -> dict[track_id, CandidateItem]`: one-to-one; first by position when the item count equals the release's track count on that disc, else by normalized title + duration within tolerance (`max(3 s, 2 %)`, `length_ms` NULL → title only); greedy by smallest duration error, no item used twice.
- `score(candidate, tracks, mapping) -> float` exactly per spec: coverage×100 − 2×mean abs seconds error + quality (+30 lossless, +15 ≥256 kbps, +5 ≥160) + reliability (+10 official album).
- Coverage threshold: album wants 1.0, track wants >0 (config later; constants now in one place).

### `sources/ytmusic.py`
- `YTMusicSource(ytm: YTMusic, downloader: Downloader, cookies: Path | None)`.
- Album search: `search(f"{albumartist} {album}", filter="albums")`, top 5; for each `get_album(browseId)`; accept only when `norm_artist` of an album artist equals the release's album artist credit (any credited artist), and track count equals the release track count (single-disc) or the sum across discs. Items from `tracks[].videoId`, `duration_seconds`, `title`, `trackNumber`. `official=True` for these.
- Track search (for `scope=track` or when no album matches): `search(f"{artist} {title}", filter="songs")`, top 5, accept by `norm_artist`, `norm_title`, duration tolerance; `official=False` unless `resultType == "song"` from an album the API names.
- `Downloader` protocol: `download(video_id, into) -> Path`. `YtDlpDownloader` uses `yt_dlp.YoutubeDL` with `format=bestaudio`, `postprocessors=[{"key":"FFmpegExtractAudio","preferredcodec":"best"}]`, `outtmpl` into the staging dir, `cookiefile` when set, `noplaylist`, `quiet`. Returns the produced file (`.opus`, `.m4a`, …). Never re-encodes.
- Wall detection (`is_wall(exc) -> bool`, pure, table-tested on real message samples): HTTP 403/429 in the message, "Sign in to confirm", "not a bot", "confirm you’re not a bot" (both apostrophes). On wall raise `SourceWall`.
- Codec/bitrate/lossless of a `Fetched` come from `ffprobe`, not from the YouTube metadata.

### `verify/probe.py`
`probe(path) -> Probe(codec, sample_rate, channels, duration_ms, bitrate)` via `ffprobe -v error -print_format json -show_streams -show_format`; `decode_ok(path) -> bool` via `ffmpeg -v error -i <path> -f null -` (any stderr line = not ok).

### `verify/fingerprint.py`
`fingerprint(path) -> (duration_s:int, fp:str)` via `fpcalc -json`. `AcoustId.lookup(duration, fp) -> list[AcoustResult(score, recording_ids)]` via `post_form("https://api.acoustid.org/v2/lookup", {client, duration, fingerprint, meta:"recordingids"})`.

### `verify/policy.py` (pure)
`decide(planned: Track, probe, acoustid_results, candidate_reliable: bool, folder_has_acoustid_reject: bool, resolve_same: Callable[[str], bool]) -> Decision(accept: bool, verified_by, reason)` implementing the spec table and duration rule exactly, including: `length_ms` NULL → only AcoustID; tracks < 30 s → duration rule; corrupt or out-of-tolerance (> max(3 s, 2 %)) → `rejected_probe`. `resolve_same(recording_id)` is backed by a MusicBrainz recording lookup (merged MBIDs resolve to the canonical id; also compare ISRCs) and is only called for results that do not contain the planned id. "Reliable" = album candidate with full coverage, or official YTM album with equal track count.

### `tag/mapping.py` + `tag/writer.py`
- `fields_for(release, track) -> TagFields` (dataclass with every field of the spec's table, multi-valued where the spec says so; `original_date` from the release group's first release date; `genres` from release then artist, max 3, then `[genres.map]` and `[genres.artist]` from config — add both tables to config, default empty).
- `write(path, fields, cover_jpeg: bytes | None, lyrics: str | None)` dispatching on mutagen type: FLAC (Vorbis + PICTURE block), OggOpus/OggVorbis (Vorbis + `METADATA_BLOCK_PICTURE` base64), MP4 (atoms and `----:com.apple.iTunes:` freeforms), MP3 (ID3v2.4, `UFID:http://musicbrainz.org`, `TXXX:` names per spec). Removes existing tags first (YouTube files carry junk).
- `tests/fixtures/picard_mapping.json`: the subset of Picard's table the writer uses, keyed by our field → per-format name; a test asserts writer output names equal the JSON for each format, by writing then reading back with mutagen.

### `tag/cover.py`
`fetch_cover(release_mbid, release_group_mbid) -> Cover(full: bytes, embed: bytes) | None`: `/release/<mbid>/front` and `/front-1200`; 404 → try `/release-group/<mbid>/front` and `/front-1200`; 404 again → None. `cover.jpg` written once per album folder.

### `tag/lyrics.py`
`fetch_lyrics(artist, title, album, duration_s) -> Lyrics(synced: str | None, plain: str | None) | None` via `GET https://lrclib.net/api/get`; 404 → None. Writer embeds `plain` or synced stripped of timestamps; `.lrc` sidecar written next to the file when `synced` exists.

### `tag/replaygain.py`
`track_gain(path)` and `album_gain(folder)` via `rsgain custom -s i -o s [-a] <files>`. `doctor` runs `rsgain custom --help` and fails if `-o`/`--opus-mode` is absent. Tests (tools): after `track_gain` an Opus file has `R128_TRACK_GAIN` and no `REPLAYGAIN_*`; a FLAC has `REPLAYGAIN_TRACK_GAIN`.

### `library/layout.py` (pure)
`render(template, fields, ext, disc_count) -> PurePath` with safe names (Windows-forbidden `<>:"/\|?*` and control chars → `_`, reserved names get `_`, trailing dots/spaces trimmed, component ≤120, whole relative path ≤240 by trimming the title then the album), `disc_prefix`, `disambiguation`, `original_year`. `resolve_collision(path, release_mbid, existing: Callable[[PurePath], str | None])` appends ` [<8 chars mbid>]` to the album folder when a different release already owns the case-insensitive path.

### `library/place.py`
- Staging dir `<library_root>/.trawl-staging/` created on demand; on Windows set the hidden attribute (`ctypes.windll.kernel32.SetFileAttributesW`, guarded by `sys.platform`).
- `place(staged, final) -> bool`: `os.replace`; `PermissionError` → False (caller sets `next_try_at` = now + backoff, state stays `tagged`).
- `cleanup_staging(conn)`: delete staging files not referenced by any track in `fetched`/`verified`/`tagged`.
- Stage paths are recorded in `tracks.path` while in `fetched`/`verified`/`tagged`, final path after `done`.

### `runner/run.py`
`async def run_once(conn, cfg, secrets, deps) -> RunReport`, `deps` bundling http, mb, sources, acoustid, tools (for test injection).
1. `cleanup_staging`.
2. Reuse pass: for each `planned` track whose `recording_mbid` has a `done` track with an existing file, copy into staging, transition `planned → verified` (`verified_by=reuse`), continue at tagging.
3. Group remaining `planned` tracks (and `missing` whose `next_try_at` passed — only via `retry`/watch, not here) by release. For each release with its tracks: transition each `planned → searching`; for each source in order (skip paused per `source_state`): `search`, score, sort; for each candidate: `fetch` the mapped items into staging (`searching → fetched`), probe + verify each (`fetched → verified` or back to `searching` with an `attempts` row); stop when all tracks of the group are verified or candidates run out. Tracks left in `searching` after all sources → `missing` (attempt `no_candidates` when nothing was found at all). `SourceWall` → write `source_state` (backoff 15 min doubling to 24 h, reset on success), leave the group's tracks `planned` with `next_try_at`, move on.
4. Tagging for each `verified`: cover (once per release), lyrics, `write`, `track_gain` → `tagged`.
5. Placement: render path, collision check, `place` → `done` with final path; `PermissionError` → retry later.
6. Album gain: for each release whose tracks are all `done`, run `album_gain` on its folder (retry later on PermissionError).
7. Concurrency: releases processed with a semaphore of `sources.ytmusic.concurrency`; MusicBrainz/AcoustID/LRCLIB/CAA through their limiters.
Every state change goes through `db.states.transition`; extend `ALLOWED` only if the spec's table requires it (report any change).

### CLI
- `run [--once]` (in this step `--once` is the only mode; `--watch` arrives in step 6 — reject it with that message). Takes the lock. Prints a rich summary: per state counts, per source accepted/rejected, walls.
- `retry [--skipped]`: `missing` (and `skipped`) → `planned`.
- `skip <track|release>` and `pick <track> <youtube or ytmusic url | local path>` (`pinned_candidate`), per spec; `pick` acceptance of an AcoustID-rejected file is TUI-only (step 7) — in CLI print the rejection and keep the track `missing`.
- `doctor`: binaries now required (non-zero exit when missing), rsgain flag check, `acoustid_key` present, staging dir writable on the library volume.
- `explain` shows attempts with source, candidate ref, score, outcome, reason.

## Acceptance

1. Gate passes; quote the last 20 lines of each command.
2. End-to-end test with fake downloader + fixture AcoustID: one 3-track release planned → `run_once` → three files in a temp library at the templated paths with tags (read back), `cover.jpg`, ReplayGain tags, all tracks `done`, attempts recorded; a second release sharing a recording reuses the file (`verified_by=reuse`); a wrong-recording AcoustID result leads to `rejected_verify` and `missing`.
3. Live run (only if `secrets.toml` with `acoustid_key` exists at the path the director gives; otherwise say so and stop at 2): temp config/data/library (never `D:/music`), `trawl add <one short album MusicBrainz link the director gives> && trawl plan && trawl run --once && trawl status`, then dump tags of one file with mutagen and `ffprobe` its codec to prove no re-encode. Quote outputs.
4. List files created/modified, deviations, and the line you are least sure of.
