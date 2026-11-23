# trawl steps 3–4: folder sources (Soulseek, own files), edition switch, lossless upgrade

**Goal:** two more sources that deliver whole album folders — `soulseek` through slskd and `local` (purchased files, rips) — with lossless preferred, a switch to another edition of the same release group when a folder matches it better, lossless repack of WAV/AIFF/APE/WavPack, and replacement of lossy library files when a verified lossless version appears.

**Spec:** `docs/specs/2026-11-23-trawl-design.md` — Источники (Soulseek, local, audio files, folder matching, upgrade), План (смена издания), Данные (transitions), Проверка (duration rule clause 3), Библиотека. The spec wins; report conflicts. Read the step 1–2 code first (`sources/base.py`, `sources/score.py`, `runner/run.py`, `verify/*`, `library/*`, `db/states.py`).

**Gate:** `uv run ruff check` · `uv run ruff format --check` · `uv run pyright` · `uv run pytest -q`

---

## Shared: folder candidates (`sources/folder.py`, pure + small I/O)

- `AUDIO_EXTS = {".flac",".opus",".ogg",".m4a",".mp3",".wav",".aiff",".aif",".ape",".wv"}`; `TAGGABLE = {".flac",".opus",".ogg",".m4a",".mp3"}`.
- `FolderFile(rel_path: PurePosixPath, size: int, length_ms: int | None, bitrate: int | None, ext: str)`.
- `group_disc_subfolders(files) -> list[FolderFile]`: a candidate folder's direct subfolders named like `CD1`, `CD 2`, `Disc 1`, `Disk2`, `D1` (case-insensitive regex) are parts of the same release; disc number taken from the name.
- `pick_format_set(files)`: when the same tracks exist in two formats (same stem or same count + matching durations), keep the lossless set.
- `reject_cue_image(files) -> str | None`: one audio file + a `.cue` → reason `cue image not supported`.
- `match_folder(tracks, files) -> dict[track_id, FolderFile]` one-to-one: by disc + leading track number in the filename when counts per disc match, else by normalized title in filename + duration tolerance; reuses `score.match_items` rules where possible (extend it rather than duplicating).
- After download, the fingerprint pass in the runner can re-map files to tracks: if AcoustID says file A is track 5's recording, the mapping follows AcoustID (spec: "сначала по отпечатку").
- Duration rule clause 3: within one folder candidate, if any file got `rejected_verify`, the duration rule is disabled for the other files of that folder (thread `folder_has_acoustid_reject` through the runner).

## Lossless repack (`library/repack.py`)

- `repack_needed(probe) -> bool` for `.wav/.aiff/.aif/.ape/.wv`.
- Allowed only when `ffprobe` shows integer PCM ≤ 32-bit (`pcm_s16le`, `pcm_s24le`, `pcm_s32le`, big-endian AIFF variants, `ape`), or WavPack that is not hybrid. Hybrid WavPack detection: a `.wv` whose `.wvc` sibling is absent AND whose ffprobe reports a lossy/hybrid profile or a bitrate far below PCM (< 0.6 × sample_rate × channels × bits) → reject `hybrid wavpack`. Float PCM (`pcm_f32le`, `pcm_f64le`) → reject `float pcm`.
- `repack(src, dst)`: `ffmpeg -i src -map 0:a -c:a flac -compression_level 8 dst`, then verify bit-exactness by comparing `ffmpeg -i X -map 0:a -f md5 -` of source and result (PCM md5 must match), else reject.
- Config `sources.lossless_repack = true`.

## Step 3: `sources/soulseek.py`

slskd REST API (https://github.com/slskd/slskd/blob/master/docs/api.md and the OpenAPI at `/swagger` of a running instance). Auth header `X-API-Key: <secrets.slskd_api_key>`, base URL from `secrets.slskd_url` (default `http://localhost:5030`).
- Search: `POST /api/v0/searches` `{id: uuid4, searchText}`; poll `GET /api/v0/searches/{id}` until `isComplete` or timeout (config `search_timeout_s = 30`); responses via `GET /api/v0/searches/{id}/responses`. Delete the search afterwards (`DELETE /api/v0/searches/{id}`).
- Queries in order, stop at the first with ≥1 acceptable folder: `"<albumartist> <album>"`, the same without edition suffixes (`norm_title` rules but keep original casing), `"<album>"` alone when the album title has ≥2 words.
- Responses → folders: each response has `username`, `hasFreeUploadSlot`, `uploadSpeed`, `files[]` (`filename` with `\` separators, `size`, `bitRate`, `length`, `extension`) and `lockedFiles[]` (ignored). Group by `(username, parent dir)`, merging disc subfolders per `group_disc_subfolders`.
- Candidate per folder: `match_folder` → coverage; `score.score` with quality from extension/bitrate (`.flac` etc. lossless) and +5 for a free slot; ties by upload speed. Drop folders below the coverage threshold.
- Track wants also accept single-file matches (artist and title in the filename, duration tolerance).
- Fetch: `POST /api/v0/transfers/downloads/{username}` with `[{filename, size}]` for every mapped file (plus nothing else). Poll `GET /api/v0/transfers/downloads/{username}`; a file is done when its state contains `Completed, Succeeded`; failed when `Completed` with `Errored|Rejected|Cancelled|TimedOut`. Stall: no byte progress for `stall_timeout_s` (default 600) → cancel all its files (`DELETE …/{id}?remove=true`) and fail the candidate (attempt `fetch_failed`, reason `stalled`).
- Downloaded files land in slskd's download dir (`sources.soulseek.download_dir`, required when the source is enabled); slskd puts them under `<download_dir>/<last remote dir name>/<file>`. Move them into staging (copy+delete across volumes) and record the staged path.
- Source-level failures (connection refused, 401) → `SourceWall`-like pause through `source_state` with reason; the queue continues with other sources.
- Concurrency `sources.soulseek.concurrency = 3`.
- Tests: recorded-shape fixtures for search responses (hand-written from the API docs, including Windows paths, locked files, CD1/CD2, mixed mp3+flac, cue image, extra files) and transfer polling sequences (success, error, stall with a fake clock). No live slskd in tests.

## Step 3: edition switch (`plan/switch.py`)

- Folder sources search with the planned release but may score folders against every edition of the release group (`mb.release_group_releases`, cached). If another edition scores strictly higher and **no track of the album is `done`**, call `switch_edition(conn, release_old, release_new)`: delete `planned/missing/skipped` tracks not in the new edition (repo helper), insert the new edition's missing tracks as `planned`, move `want_tracks` links (by `recording_mbid`), update `releases` (`choice_reason.switched_from`, `switched_reason`), all in one transaction; then the runner retries the group with the new edition. If any track is `done`, score only against the current edition.
- Tests: switch happens; switch blocked by a `done` track; want links preserved; `explain` shows the switch.

## Step 3: lossless upgrade (`runner/upgrade.py`)

- `upgrade_pass(conn, deps, older_than_days=14)`: for releases with ≥1 `done` track with `lossless=0` and last upgrade check older than the threshold (store per release `upgrade_checked_at`, new migration), search lossless-capable sources only (`soulseek`, `local`), accept only candidates whose mapped files are lossless and verified by AcoustID or the duration rule, stage+tag, then atomically replace each library file (`done → done`, new `codec/lossless/bitrate`, new path if the extension changed — delete the old file only after the new one is placed), then album gain for the folder.
- Exposed as `trawl upgrade [--now]` in this step (`--now` ignores the age threshold); `run --watch` will call it in step 6.
- Tests: an opus track gets replaced by a FLAC with a new path and the old file removed; a lossy candidate never replaces; a PermissionError leaves the old file and retries.

## Step 4: `sources/local.py`

- Config `sources.local.inbox = ["…"]` (list of dirs), `sources.local.move = false`.
- Every direct subfolder of an inbox dir is a folder candidate (disc subfolders merged); files read with `mutagen`/`ffprobe` for length/bitrate; `search` filters folders by normalized artist/album appearing in the folder name or in the files' existing tags (artist+album), then `match_folder`.
- Fetch = copy (or move when `move = true`) into staging; originals are never modified.
- Plus the inverse use: `trawl import <dir>` scans a folder of own files, fingerprints them, resolves recordings via AcoustID → MusicBrainz, picks the release by the planning rules among releases containing ≥ 80 % of the folder's recordings, creates a `manual` want with `origin_ref = "import:<dir>"`, plans it and lets the runner take files from that folder as a pinned `local` candidate.
- Tests: folder found by name, by tags; copy vs move; `import` resolves a 3-file folder generated with ffmpeg and fixture AcoustID/MB responses.

## Config additions (document all in `config.example.toml`)

```toml
[sources]
order = ["local", "soulseek", "ytmusic"]
lossless_repack = true

[sources.soulseek]
download_dir = ""          # required when soulseek is in order
concurrency = 3
search_timeout_s = 30
stall_timeout_s = 600

[sources.local]
inbox = []
move = false

[upgrade]
older_than_days = 14
```

Secrets: `slskd_url`, `slskd_api_key`.

`doctor`: slskd reachable and authorized (`GET /api/v0/application`), `download_dir` exists, inbox dirs exist.

## Acceptance

1. Gate passes, 0 skipped locally; quote last 20 lines of each command.
2. Offline end-to-end: a fake slskd (httpx MockTransport) serving a FLAC folder generated with ffmpeg (CD1/CD2, an extra `.log`, a `.jpg`) → two-disc release fully `done` as FLAC with correct disc/track tags; the same release first fetched lossy from a fake ytmusic then upgraded by `trawl upgrade --now`.
3. Live: only if `secrets.toml` with slskd keys exists at the director-given path; otherwise say so. Never touch `D:/music`.
4. Files, deviations, least-sure line.
