# trawl steps 5–6: taste importers and automation

**Goal:** wants arrive by themselves — from ListenBrainz listening stats, Yandex Music and Spotify likes/playlists, and new releases of followed artists — and `trawl run --watch` keeps the library in sync on a schedule, installable as a Windows scheduled task or a systemd unit.

**Spec:** `docs/specs/2026-11-23-trawl-design.md` — Вкус → желания и ссылки (resolve chain, sources, likes.scope, tiers), Данные (`unresolved`, `cursors`, `artists.followed`), Интерфейс (`sync-taste`, `follow`, `unfollow`, `link`, `run --watch`, `add` with foreign links), Риски. The spec wins; report conflicts. Read the existing code first.

**Gate:** `uv run ruff check` · `uv run ruff format --check` · `uv run pyright` · `uv run pytest -q`

New deps: `yandex-music`. Spotify and ListenBrainz through `HttpClient` (no SDKs).

---

## Step 5

### `resolve/` (pure decisions + MB calls)
- `ExternalTrack(origin, origin_ref, artist, title, album, length_ms, isrc, upc)`.
- `resolve_track(mb, t) -> Resolved(recording_mbid) | Unresolved(reason)` per spec order: ISRC search (`recording?query=isrc:<isrc>`; several → closest length within `max(3 s, 2 %)`); text search (`recording?query=artist:"…" AND recording:"…"` + album when known): accept only MB score ≥ 90, `norm_artist` equal to any credited artist, length within tolerance (skip the length check when either side is unknown).
- `resolve_album(mb, artist, title, upc)`: UPC via `release?query=barcode:<upc>`; else release-group search with the step-1 acceptance rule.
- Unresolved rows upsert into `unresolved` by `(origin, origin_ref)` with `last_try_at`; already-resolved refs are skipped on later syncs (keep a `resolved_refs` cursor or a column — pick one, migration).
- `trawl link <unresolved id> <musicbrainz link>` creates the want with the row's origin and deletes the row.
- `add` with Spotify / Yandex / YouTube Music links now resolves through the same code (track → recording want; album → release_group want; artist → artist want; playlist → one want per track). Remove the step-1 "supported from step 5" refusal.

### `taste/listenbrainz.py`
- Config `[taste.listenbrainz] user = "", range = "all_time"` (`this_week|this_month|this_year|all_time`), optional secret `listenbrainz_token`.
- `GET https://api.listenbrainz.org/1/stats/user/{user}/artists?range=…&count=100` paginated by `offset` until exhausted (and `/recordings` likewise). 204 = stats not computed yet → message, no error.
- Tiers from config `[[taste.tiers]]` (`min_plays`, `scope`, `limit`), defaults = spec (≥6 → discography 14, ≥3 → 10, 2 → 3, 1 → 1). Artist wants carry `limit`; discography order uses the user's per-release-group play counts from the recordings stats (`plays` mapping introduced in step 1's sort function), ties newest first.
- Recordings with plays ≥ `taste.listenbrainz.min_track_plays` (default 3) become recording wants (likes semantics).
- Re-sync updates `limit` of existing artist wants when the tier changed (only upward; never deletes).

### `taste/yandex.py`
- Secret `yandex_token`; config `[taste.yandex] playlists = []` (kinds or titles), `likes = true`.
- `yandex_music.Client(token).init()`, liked tracks via `users_likes_tracks()` then `fetch_tracks`, playlists via `users_playlists_list` + `users_playlists`. Map to `ExternalTrack` (artists joined, first album title, `duration_ms`; ISRC if the object exposes it).
- Cursor: last seen like timestamp; only new likes are processed after the first sync.
- The library is sync; run it in a thread.

### `taste/spotify.py`
- Secrets `spotify_client_id`, `spotify_refresh_token`. `trawl auth spotify` runs PKCE: local loopback server on `http://127.0.0.1:8888/callback`, opens the browser, exchanges the code, stores the refresh token in `secrets.toml` (rewrite only that key, preserve the rest; file permissions 600 on Linux).
- Liked tracks: `GET /v1/me/tracks?limit=50` paginated; playlists from config by id: `GET /v1/playlists/{id}/items`. ISRC from `track.external_ids.isrc`; album UPC from `album.external_ids.upc` when present. No batch endpoints. Access token refreshed on 401 once.
- Cursor: `added_at` of the newest processed like.

### `likes.scope`
`"track"` → `kind=recording, scope=track`; `"album"` → `kind=release_group, scope=album` of the group chosen by the planning rules for that recording.

### CLI
`sync-taste [listenbrainz|yandex|spotify|releases|all]`, `link`, `auth spotify`, `unresolved` list in `status` (count) and `trawl unresolved` (table). `doctor` checks each enabled taste source's credentials (Spotify token refresh, Yandex `account_status`, ListenBrainz user exists) — skip with `--offline`.

### Tests
Recorded-shape fixtures for ListenBrainz stats (incl. 204), Spotify pages (liked, playlist, 401 → refresh), Yandex objects (built with the library's `de_json` from fixture dicts, no network), MB ISRC/barcode/text searches. Table tests for `resolve_track` decisions (ISRC hit, ISRC multiple, text below 90, artist mismatch, length mismatch, unknown length), tier mapping, cursors (second sync processes only new items), `link`.

## Step 6

### Follow and new releases (`taste/releases.py`)
- `trawl follow <artist link|name>` / `unfollow`: sets `artists.followed` (upsert artist from MB). Artists wanted at tier ≤ `taste.follow_max_tier` (default 2) by ListenBrainz are followed automatically.
- `sync-taste releases`: for followed artists, browse release groups (`type` from `[taste.releases] types = ["album","ep"]`), keep those with `first_release_date` > `releases_checked_at` (first run: only the last 90 days, so following an artist does not re-want the whole discography), create `release_group` wants with `origin=releases`, then set `releases_checked_at = now`.

### `run --watch` (`runner/watch.py`)
- Config `[watch]`: `taste_every = "6h"`, `releases_every = "24h"`, `queue_every = "15m"`, `upgrade_every = "24h"`, `retry_missing_days` (exists). Durations parsed as `<n>[smhd]`.
- One process holding the lock; loop: due jobs in order sync-taste(all enabled) → releases → plan → run_once → retry missing older than `retry_missing_days` → upgrade_pass; each job's last run stored in `cursors` so restarts keep the schedule; sleep until the next due job (interruptible by Ctrl+C / SIGTERM with a clean shutdown that finishes the current file transition).
- A failing job logs and the loop continues; a job never runs concurrently with itself.

### Service install
- `trawl service install|uninstall|status`:
  - Windows: Task Scheduler task `trawl` at logon, running `<uv tool path or sys.executable> -m trawl run --watch` hidden, restart on failure (via `schtasks /Create /XML` with a generated XML).
  - Linux: user systemd unit `~/.config/systemd/user/trawl.service` (`Restart=on-failure`), `systemctl --user enable --now trawl`.
- Tests: generated XML/unit content (pure), command lines; no real install in tests.

### Tests
Watch loop with a fake clock and fake jobs: ordering, persistence across restart, failure isolation, shutdown. Releases: first-run window, cursor update, type filter.

## Acceptance

1. Gate passes, 0 skipped; last 20 lines of each command quoted.
2. Live, only for credentials present in `C:/Users/Anek/.config/trawl/secrets.toml` (copy into a temp config; never print): `sync-taste` per available source with counts of wants and unresolved. Without credentials, say which were missing. Never touch `D:/music`.
3. Files, deviations, least-sure line.
