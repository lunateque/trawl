# trawl

[![CI](https://github.com/lunateque/trawl/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/lunateque/trawl/actions/workflows/ci.yml)

Plan-first music acquisition into a tagged local library.

Every track is resolved to a specific [MusicBrainz](https://musicbrainz.org/) release and recording before anything is searched or downloaded. Sources only supply audio; metadata is never guessed from filenames or video titles.

```
taste -> wants -> plan (MusicBrainz release + recording MBIDs)
                    |
                    v
         sources by priority: local -> soulseek (slskd) -> ytmusic
                    |  candidate
                    v
         fetch -> verify (ffprobe + AcoustID) -> tag + ReplayGain -> library
                    |  rejected
                    +--> next candidate / next source
```

## How it works

- **Taste & wants** - imports listening history and likes from ListenBrainz, Yandex Music, Spotify (OAuth PKCE), followed artist releases, or manual `trawl add`.
- **Deterministic planning** - resolves external tracks via ISRC, UPC, or normalized artist/title/duration matching; selects canonical releases and editions deterministically and records `choice_reason`.
- **Verification before placement** - decodes every downloaded file via `ffprobe`/`ffmpeg` to catch corruption, fingerprints via Chromaprint (`fpcalc`) + AcoustID, and rejects mismatched recordings or remasters.
- **No transcoding** - preserves original streams (FLAC, Opus, M4A, MP3, Ogg) and bit-exact repacks integer PCM WAV/AIFF/APE into FLAC when needed.
- **Picard-compatible tagging** - writes MusicBrainz IDs, multi-artist credits, dates, ISRC, Cover Art Archive artwork, LRCLIB synced `.lrc` lyrics, and RFC 7845 / ReplayGain tags (`rsgain`), then moves atomically from staging into the library.
- **SQLite WAL state machine** - conditional state transitions (`UPDATE ... WHERE id=? AND state=?`) with OS file locking so CLI, background `--watch` runner, and the Textual TUI stay consistent without clobbering manual decisions.

## Requirements

- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/)
- External tools on `PATH`: `ffmpeg`, `ffprobe`, `fpcalc` (Chromaprint), `rsgain`
- Optional: `slskd` for Soulseek

## Quick start

```sh
uv sync --frozen
uv run trawl doctor          # verify ffmpeg, fpcalc, rsgain, config, and library permissions
```

Copy [`config.example.toml`](config.example.toml) to your user config directory (or pass `--config` / `TRAWL_CONFIG`), put API keys in a sibling `secrets.toml` (at minimum `acoustid_key`), and run:

```sh
uv run trawl add "Radiohead - OK Computer"
uv run trawl plan
uv run trawl run --once      # or: uv run trawl run --watch
uv run trawl tui             # live queue, source backoff state, manual pick/skip
```

## Commands

| Command | Purpose |
| --- | --- |
| `trawl doctor` | Check external audio tools, keys, slskd connectivity, and paths |
| `trawl add <query-or-url>` | Add a manual artist, album, or track want |
| `trawl sync-taste` | Pull wants from ListenBrainz, Yandex Music, Spotify, or new releases |
| `trawl follow` / `unfollow` | Track new releases for an artist |
| `trawl plan [--refresh]` | Expand wants into MusicBrainz releases and tracks |
| `trawl run [--once\|--watch]` | Process queue, retry missing tracks, upgrade lossy to lossless |
| `trawl status` / `explain` | Inspect queue state, source pauses, and per-track decision history |
| `trawl pick` / `skip` / `retry` | Manually pin a candidate URL/path, skip, or requeue |
| `trawl tui` | Textual terminal UI for monitoring and manual overrides |

## Architecture & checks

Strict layered package boundaries enforced in CI via `import-linter` (`cli`/`tui` -> `runner` -> `taste` -> `plan`/`resolve` -> `sources`/`tag` -> `library` -> `verify` -> `mb` -> `net`/`db` -> `log` -> `config` -> `core`).

```sh
uv run ruff check .
uv run ruff format --check .
uv run lint-imports
uv run python scripts/check_module_size.py
uv run pyright
uv run ty check
uv run pytest -q -rs --cov
```
