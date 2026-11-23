# trawl step 1: foundation — implementation plan

**Goal:** a working `trawl` CLI that turns wants (MusicBrainz links or text) into planned releases and tracks in its own SQLite, and explains every decision. No downloading yet.

**Spec:** `docs/specs/2026-11-23-trawl-design.md` (Russian). Sections that matter here: Технологии, Раскладка пакета, Данные (tables, statuses, transitions), Вкус → желания (only `add` and the shared title normalization), План, Конфиг и секреты, Исполнение (retries, limiter), Интерфейс (`add`, `plan`, `status`, `explain`, `doctor`), Тестирование. When the plan and the spec disagree, the spec wins; report the disagreement.

**Stack:** Python 3.12, uv, httpx (async), typer, rich, platformdirs, stdlib sqlite3/tomllib. Dev: ruff, pyright (strict), pytest, pytest-asyncio.

**Gate (run from repo root, all must pass):**

```
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest -q
```

---

## Rules for the whole step

- Code, identifiers, comments, commit-free: English. Comments only where the reason is not obvious.
- No network in tests. HTTP goes through `httpx.MockTransport`. MusicBrainz fixtures are real responses recorded once by `tests/fixtures/record_mb.py` (live, rate-limited 1 req/s, User-Agent with `me@lunateque.com`), stored as JSON under `tests/fixtures/mb/`, and committed. Pick fixtures that exercise the rules: a release group with several editions (e.g. Radiohead "OK Computer"), a multi-disc release, a release with a pregap/data track if you can find one quickly (otherwise hand-edit a copy of a fixture and name it `*_synthetic.json`), a recording present on an album and a compilation, a Cyrillic artist (e.g. "Кино").
- Pure logic (choice, normalization, transitions, URL parsing) is in functions without I/O, tested table-style.
- pyright strict must pass without `# type: ignore` except at untyped third-party boundaries, each with a reason comment.
- Do not implement anything from later steps (sources, verification, tags, taste importers, TUI, lock, `run`). Links from Spotify/Yandex/YouTube Music in `add` exit with code 2 and the message `links from <service> are supported from step 5; use a MusicBrainz link or text`.

## File structure

```
pyproject.toml                 project, deps, ruff, pyright, pytest config; script entry trawl = "trawl.cli:app"
uv.lock
config.example.toml            every key of step 1 with comments
.github/workflows/ci.yml       windows-latest + ubuntu-latest: setup uv, uv sync --locked, the gate
.omp/route.yml                 gate list (already written by the director, keep)
src/trawl/__init__.py          __version__ = "0.1.0"
src/trawl/cli.py               typer app: add, plan, status, explain, doctor
src/trawl/config.py            Config dataclasses, load(), ConfigError
src/trawl/normalize.py         norm_title(), norm_artist()
src/trawl/log.py               JSON-lines file log + rich console handler
src/trawl/db/__init__.py
src/trawl/db/connect.py        open_db(path) -> sqlite3.Connection; migrations
src/trawl/db/migrations/0001_init.sql
src/trawl/db/states.py         TrackState, Actor, ALLOWED, transition()
src/trawl/db/repo.py           small typed query helpers used by plan/cli
src/trawl/net/__init__.py
src/trawl/net/limiter.py       RateLimiter
src/trawl/net/client.py        HttpClient, HttpError
src/trawl/mb/__init__.py
src/trawl/mb/models.py         frozen dataclasses parsed from MB JSON
src/trawl/mb/client.py         MusicBrainzClient (cached)
src/trawl/plan/__init__.py
src/trawl/plan/choose.py       choose_release_group(), choose_release()
src/trawl/plan/wants.py        parse_add_target(), want creation
src/trawl/plan/run.py          plan_new_wants()
src/trawl/doctor.py            checks
tests/...                      mirrors src
```

## Task 1: Project skeleton

- `uv init --package --name trawl` style layout under `src/`, `requires-python = ">=3.12"`.
- Runtime deps: `httpx`, `typer`, `rich`, `platformdirs`. Dev group: `ruff`, `pyright`, `pytest`, `pytest-asyncio`.
- ruff: line-length 100, `select = ["E","F","W","I","UP","B","SIM","RUF","ASYNC","PTH","S","T20","N"]`, ignore `S101` in tests.
- pyright: `typeCheckingMode = "strict"`, `include = ["src","tests"]`.
- pytest: `asyncio_mode = "auto"`, `testpaths = ["tests"]`.
- CI workflow as in the structure above; Python 3.12 via `uv python install 3.12`.
- Acceptance: the gate passes on an empty test (`tests/test_smoke.py` importing `trawl` and checking `__version__`).

## Task 2: Config

`config.py`:

```python
@dataclass(frozen=True)
class PlanConfig:
    types: tuple[str, ...] = ("album", "ep", "single")      # primary types allowed when expanding artists
    countries: tuple[str, ...] = ("XW", "US", "GB")
    prefer: Literal["original", "deluxe"] = "original"
    discography_limit: int = 14                              # manual `add <artist>` default

@dataclass(frozen=True)
class MbConfig:
    cache_days: int = 30

@dataclass(frozen=True)
class Config:
    library_root: Path
    data_dir: Path
    contact_email: str
    mb: MbConfig
    plan: PlanConfig
    source_path: Path | None   # file it was loaded from

def config_path(explicit: Path | None) -> Path   # --config, then TRAWL_CONFIG, then platformdirs.user_config_dir("trawl")/config.toml
def load(path: Path) -> Config
class ConfigError(Exception)
```

- `library_root` and `contact_email` are required. `data_dir` defaults to `platformdirs.user_data_dir("trawl")`.
- Unknown keys at any level raise `ConfigError` naming the dotted key path (`plan.prefr`). tomllib gives no line numbers; the dotted path is the locator.
- Wrong types raise `ConfigError` with the key path and the expected type. `prefer` outside its two values, empty `contact_email` or one without `@`, `discography_limit < 1`, `cache_days < 0` are errors.
- Missing file: `ConfigError` saying where it looked and to copy `config.example.toml` there.
- `config.example.toml` documents every key with the defaults above; `library_root = "D:/music"`.
- Tests: valid minimal, valid full, each error case, path resolution order (monkeypatch env).

## Task 3: Normalization

`normalize.py`, one implementation for the whole project:

- `norm_title(s)`: NFKC, casefold, strip edition suffixes matching (case-insensitive) ` - (\d{4} )?remaster(ed)?( \d{4})?( version)?$`, ` \((\d{4} )?remaster(ed)?( \d{4})?\)$`, ` - live$` is NOT stripped (live is a different recording); then remove punctuation (Unicode category P*), collapse whitespace, strip.
- `norm_artist(s)`: NFKC, casefold, `&`/`and`/`и` treated equal by replacing ` & ` and ` и ` with ` and `, remove punctuation, collapse whitespace. A leading `the ` is dropped.
- No transliteration; Cyrillic and CJK pass through unchanged apart from casefold/NFKC.
- Tests: table with Latin, Cyrillic (`Кино`), Japanese (`夜が明けたら`), remaster suffixes, `Live` kept, punctuation, `The Strokes` == `Strokes`.

## Task 4: Database and transitions

`db/migrations/0001_init.sql` creates every table from the spec's Данные table exactly (wants, unresolved, artists, releases, tracks, want_tracks, attempts, source_state, cursors, mb_cache) with:
- `tracks.release_track_mbid UNIQUE`, index on `tracks.recording_mbid`, index on `tracks.state`, `tracks.release_mbid` FK → `releases.mbid`, `want_tracks` PK (`want_id`, `track_id`) with FKs `ON DELETE CASCADE`, `attempts.track_id` FK `ON DELETE CASCADE`.
- CHECK constraints for every enumerated column (`wants.kind`, `wants.scope`, `wants.origin`, `wants.status`, `tracks.state`, `tracks.verified_by`, `attempts.outcome`) with exactly the values in the spec.
- Timestamps are ISO-8601 UTC text.

`db/connect.py`: `open_db(path: Path) -> sqlite3.Connection` creates parent dirs, sets `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`, `row_factory=sqlite3.Row`, applies migrations whose number > `PRAGMA user_version` in one transaction each, then sets `user_version`. Migrations are read with `importlib.resources`.

`db/states.py`:

```python
class TrackState(StrEnum): PLANNED, SEARCHING, FETCHED, VERIFIED, TAGGED, DONE, MISSING, SKIPPED
class Actor(StrEnum): PLAN, RUNNER, USER           # USER = TUI or CLI decisions
ALLOWED: Mapping[tuple[TrackState, TrackState], frozenset[Actor]]   # exactly the spec's transition table
def transition(conn, track_id: int, src: TrackState, dst: TrackState, actor: Actor, **fields: object) -> bool
```

- `transition` raises `ValueError` if `(src, dst)` is not allowed for `actor`; otherwise runs `UPDATE tracks SET state=?, <fields> WHERE id=? AND state=?` and returns whether a row changed. Field names are validated against a fixed allow-list of track columns (no SQL built from arbitrary names).
- The spec's "любое, кроме done → skipped" expands to every non-done state. Self-transitions in the table (`done → done` for upgrade, `tagged → tagged` retry) are allowed for RUNNER. Deletion on edition switch is not a transition; it is `repo.delete_tracks()` and is out of step 1's use but write the helper only if `plan` needs it (it does not in step 1).
- Tests: every allowed pair succeeds for its actor and fails for others; a disallowed pair raises; a stale `src` returns `False` and changes nothing; migrations are idempotent (open twice, `user_version` stays 1); CHECK constraints reject a bad state.

## Task 5: HTTP client and limiter

`net/limiter.py`: `RateLimiter(min_interval: float, clock=time.monotonic, sleep=asyncio.sleep)`; `async with limiter:` guarantees starts at least `min_interval` apart across concurrent tasks (one `asyncio.Lock`).

`net/client.py`:

```python
class HttpError(Exception): status: int | None; url: str
class HttpClient:
    def __init__(self, user_agent: str, limiters: Mapping[str, RateLimiter], transport: httpx.AsyncBaseTransport | None = None, sleep=asyncio.sleep, rand=random.random, max_attempts: int = 5)
    async def get_json(self, url: str, params: Mapping[str, str] | None = None) -> Any
    async def aclose(self) -> None
```

- Limiter chosen by URL host; unknown host → no limiter.
- `follow_redirects=True`, timeout 30 s.
- Retries: network errors (`httpx.TransportError`) and 5xx with backoff `min(60, 2**attempt) * (0.5 + rand())`; 429 and 503 honour `Retry-After` (seconds or HTTP date) when present, capped at 300 s; other 4xx raise `HttpError` immediately; after `max_attempts` raise `HttpError` with the last status.
- Tests (MockTransport + fake sleep recording delays): success, 503 then success with Retry-After honoured, 429 with HTTP-date Retry-After, 500×5 → HttpError, 404 → immediate HttpError with no sleep, transport error retried, limiter spacing with two concurrent calls (fake clock).

## Task 6: MusicBrainz client and models

`mb/models.py` (frozen dataclasses, parsed by `from_json` classmethods tolerant to missing optional fields):
- `Credit(name: str, artist_mbid: str, joinphrase: str)`; `credit_string(credits) -> str`.
- `ReleaseGroupRef(mbid, title, primary_type: str | None, secondary_types: tuple[str, ...], first_release_date: str | None)`.
- `Track(mbid, recording_mbid, position: int, number: str, title, length_ms: int | None, credits: tuple[Credit, ...], isrcs: tuple[str, ...])`.
- `Medium(position: int, format: str | None, tracks: tuple[Track, ...], pregap: Track | None, data_tracks: tuple[Track, ...])`.
- `Release(mbid, title, status: str | None, country: str | None, date: str | None, credits, release_group: ReleaseGroupRef, media: tuple[Medium, ...], disambiguation: str, labels: tuple[str, ...], genres: tuple[str, ...])` with `track_count`.
- `RecordingRef(mbid, title, length_ms, credits, isrcs, releases: tuple[ReleaseRef, ...])` where `ReleaseRef(mbid, status, country, date, release_group: ReleaseGroupRef)`.
- `ArtistRef(mbid, name, sort_name, score: int | None)`; search results carry MB `score`.

`mb/client.py`:

```python
class MusicBrainzClient:
    BASE = "https://musicbrainz.org/ws/2"
    def __init__(self, http: HttpClient, conn: sqlite3.Connection, cache_days: int, now=lambda: datetime.now(UTC))
    async def release(self, mbid) -> Release                 # inc=recordings+artist-credits+release-groups+media+isrcs+labels+genres
    async def release_group_releases(self, rg_mbid) -> list[Release]   # browse release?release-group=…&inc=media+recordings+artist-credits+release-groups, paginated (limit 100)
    async def recording(self, mbid) -> RecordingRef          # inc=releases+release-groups+artist-credits+isrcs
    async def artist_release_groups(self, artist_mbid, types) -> list[ReleaseGroupRef]  # browse release-group?artist=…&type=…, paginated
    async def search_artists(self, query) -> list[ArtistRef]
    async def search_release_groups(self, artist, title) -> list[tuple[ReleaseGroupRef, int, str]]  # (group, score, artist credit string)
```

- Always `fmt=json`. Cache: key = final URL with sorted params; fresh if `fetched_at` within `cache_days`; `refresh=True` attribute on the client bypasses reads (still writes).
- MB limiter: 1.0 s interval for host `musicbrainz.org`; User-Agent `trawl/<version> ( <contact_email> )`.
- Tests on recorded fixtures: parsing each fixture; multi-disc counts; pregap/data tracks land in their own fields; pagination concatenates (MockTransport serving two pages); cache hit makes no HTTP call; stale cache refetches.

## Task 7: Release choice (pure)

`plan/choose.py`:

```python
def choose_release_group(groups: Sequence[ReleaseGroupRef], has_official: Callable[[str], bool]) -> ReleaseGroupRef | None
def choose_release(releases: Sequence[Release], cfg: PlanConfig) -> tuple[Release, dict[str, object]] | None
```

- Group: earliest `first_release_date` among groups with primary type Album that have an official release; else EP; else Single; compilations (`"Compilation" in secondary_types`) only after all non-compilations; missing dates sort last; tie → smaller MBID.
- Release keys in order (spec, План): Official first; medium format Digital Media > CD > other (a release's format = its first medium's format); country index in `cfg.countries` with `XW` handled as just another entry (default list starts with it), unknown countries after listed ones; `prefer=original` → fewer tracks first, `deluxe` → more; earlier date (missing last); smaller MBID.
- Reason dict: `{"status":…, "format":…, "country":…, "track_count":…, "date":…, "compared": n, "prefer": cfg.prefer}`.
- Tests: each key decides a case on its own (other keys equal), ties fall to MBID, empty input → None, compilation vs album, missing dates.

## Task 8: Wants, `add` and planning

`plan/wants.py`:
- `parse_add_target(text) -> Target` where `Target` is one of `MbLink(kind, mbid)` (`musicbrainz.org/(artist|release-group|release|recording)/<uuid>`, with or without `https://`, `beta.`), `ForeignLink(service)` (open.spotify.com, music.yandex.*, music.youtube.com, youtube.com, youtu.be), `TextAlbum(artist, title)` (split on the first ` - ` or ` — `), `TextArtist(name)`.
- `create_want(conn, kind, mbid, scope, origin, origin_ref, limit=None) -> int`; duplicates (same kind+mbid+scope, status not dropped) return the existing id.

Text resolution (in `cli.add`, using `MusicBrainzClient` and `normalize`):
- `TextAlbum`: `search_release_groups`; auto-accept the top result only if score ≥ 90, `norm_artist` of its credit equals `norm_artist(artist)`, and `norm_title` equal; else print up to 8 numbered candidates (title, type, year, artist, score) and prompt for a number (`0` cancels). `--yes` accepts the top result regardless.
- `TextArtist`: same with `search_artists`, comparing `norm_artist(name)`.
- Default scopes: artist → `discography` with `limit = cfg.plan.discography_limit`; release_group/release → `album`; recording → `track`. `--scope` overrides; `--limit` for artists.
- After creating the want, `add` prints the want id and runs nothing else; `plan` does the planning (so `add` stays fast and offline-tolerant for links).

`plan/run.py`: `async def plan_new_wants(conn, mb, cfg) -> PlanReport` processes wants with status `new`:
- `release`: fetch release → tracks.
- `release_group`: fetch releases → `choose_release` → tracks.
- `recording`: fetch recording; if a track with this `recording_mbid` already exists in `tracks`, link the want to the earliest such track via `want_tracks` and mark the want `planned` (spec: a like does not create a new release). Otherwise pick the group with `choose_release_group` over the recording's release groups (`has_official` from the recording's releases), then `choose_release` among the recording's releases in that group, then create only the track for this recording when scope is `track`, all tracks when `album`.
- `artist`: `artist_release_groups(types=cfg.plan.types)`, exclude compilations, sort newest first (play counts arrive in step 5; keep the sort a separate function taking an optional `plays: Mapping[str, int]`), take `limit`, and for each group create a child want `kind=release_group, scope=album, origin` same as parent, `origin_ref = "artist:<mbid>"`; the parent becomes `planned`. Child wants are planned in the same run.
- Track creation: one `releases` row (upsert by mbid, with `choice_reason` JSON), one `tracks` row per track in `state=planned` (via insert, not `transition`), skipping each medium's `pregap` and `data_tracks`; `disc_track_count` = tracks on that medium; `artist_mbids` JSON list; `length_ms` may be NULL; `want_tracks` link. Existing `release_track_mbid` → just link.
- A want whose filters leave zero tracks → status `empty`. MB errors on one want → log, leave it `new`, continue with the others; the report counts them.
- Everything for one want commits in one transaction.
- Tests with MockTransport on fixtures: release want → right number of tracks and discs; group want picks the expected edition and stores the reason; recording want on an album+compilation recording picks the album; second like of the same recording links instead of inserting; artist want creates child wants up to `limit`; pregap/data tracks skipped; MB failure leaves the want `new`.

## Task 9: `status`, `explain`, `doctor`, logging, CLI wiring

- `log.py`: `setup(data_dir, verbose)`: JSON lines to `data_dir/trawl.log` (RotatingFileHandler 10 MB × 3) with `ts, level, event, **fields`; rich console handler at INFO (DEBUG with `-v`).
- `cli.py` (typer): global options `--config PATH`, `-v`. Commands:
  - `add TARGET [--scope album|track|discography] [--limit N] [--yes]`
  - `plan [--refresh]`
  - `status` — rich tables: tracks per state, wants per status and origin, `unresolved` count.
  - `explain TARGET` — TARGET is a track id, a release MBID or a MusicBrainz link; prints the wants (kind, origin, origin_ref, created_at) behind it, the release `choice_reason`, and every attempt (empty in step 1, but the section exists).
  - `doctor` — checks and prints a table: config loads; `library_root` exists or can be created and is writable (create and delete a temp file); `data_dir` writable; DB opens and migrates; `ffmpeg`, `ffprobe`, `fpcalc`, `rsgain` found on PATH with first line of `-version`/`--version` (missing = warning in step 1, they become required in step 2); MusicBrainz reachable (one GET to `/ws/2/artist?query=test&limit=1`, skip with `--offline`). Exit 1 if any non-warning check fails.
  - Exit codes: 0 ok, 1 failure, 2 usage/unsupported input.
- Tests: typer `CliRunner` with a temp config and DB and a MockTransport-backed MB client injected through a factory function (`cli.make_mb`) that tests monkeypatch: `add <mb release link>` then `plan` then `status` shows the planned count; `explain` prints the choice reason; `add <spotify link>` exits 2 with the step-5 message; `doctor --offline` with missing binaries exits 0 and prints warnings; bad config exits 1 with the key path.

## Acceptance for step 1

1. The gate passes; quote the last 20 lines of each command.
2. Manual run against live MusicBrainz in a temp data dir (config pointing `library_root` and `data_dir` into a temp folder): `trawl add "Radiohead - OK Computer" --yes`, `trawl add https://musicbrainz.org/artist/<Кино mbid> --limit 3`, `trawl plan`, `trawl status`, `trawl explain <one track id>`. Quote the output.
3. List every file created.
