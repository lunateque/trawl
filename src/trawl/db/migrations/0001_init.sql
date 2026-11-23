CREATE TABLE wants (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('artist', 'release_group', 'release', 'recording')),
    mbid TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('track', 'album', 'discography')),
    "limit" INTEGER,
    origin TEXT NOT NULL CHECK (origin IN ('listenbrainz', 'yandex', 'spotify', 'manual', 'releases')),
    origin_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('new', 'planned', 'empty', 'dropped')),
    created_at TEXT NOT NULL
);
CREATE TABLE unresolved (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL CHECK (origin IN ('listenbrainz', 'yandex', 'spotify', 'manual', 'releases')),
    origin_ref TEXT NOT NULL,
    artist TEXT,
    title TEXT,
    album TEXT,
    length_ms INTEGER,
    isrc TEXT,
    upc TEXT,
    reason TEXT NOT NULL,
    last_try_at TEXT NOT NULL
);
CREATE TABLE artists (
    mbid TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    sort_name TEXT NOT NULL,
    followed INTEGER NOT NULL DEFAULT 0,
    releases_checked_at TEXT
);
CREATE TABLE releases (
    mbid TEXT PRIMARY KEY NOT NULL,
    release_group_mbid TEXT NOT NULL,
    title TEXT NOT NULL,
    artist_credit TEXT NOT NULL,
    date TEXT,
    original_date TEXT,
    type TEXT,
    status TEXT,
    disc_count INTEGER NOT NULL,
    choice_reason TEXT NOT NULL
);
CREATE TABLE tracks (
    id INTEGER PRIMARY KEY,
    release_mbid TEXT NOT NULL REFERENCES releases(mbid),
    release_track_mbid TEXT NOT NULL UNIQUE,
    recording_mbid TEXT NOT NULL,
    disc INTEGER NOT NULL,
    disc_track_count INTEGER NOT NULL,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    artist_credit TEXT NOT NULL,
    artist_mbids TEXT NOT NULL,
    length_ms INTEGER,
    isrc TEXT,
    state TEXT NOT NULL CHECK (state IN (
        'planned', 'searching', 'fetched', 'verified', 'tagged', 'done', 'missing', 'skipped'
    )),
    pinned_candidate TEXT,
    source TEXT,
    codec TEXT,
    lossless INTEGER,
    bitrate INTEGER,
    verified_by TEXT CHECK (verified_by IN ('acoustid', 'duration', 'manual', 'reuse')),
    path TEXT,
    next_try_at TEXT
);
CREATE INDEX tracks_recording_mbid ON tracks(recording_mbid);
CREATE INDEX tracks_state ON tracks(state);
CREATE TABLE want_tracks (
    want_id INTEGER NOT NULL REFERENCES wants(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    PRIMARY KEY (want_id, track_id)
);
CREATE TABLE attempts (
    id INTEGER PRIMARY KEY,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    candidate_ref TEXT,
    score REAL,
    outcome TEXT NOT NULL CHECK (outcome IN (
        'accepted', 'rejected_verify', 'rejected_probe', 'fetch_failed', 'no_candidates'
    )),
    reason TEXT,
    at TEXT NOT NULL
);
CREATE TABLE source_state (
    source TEXT PRIMARY KEY NOT NULL,
    paused_until TEXT,
    backoff_s INTEGER NOT NULL,
    reason TEXT
);
CREATE TABLE cursors (
    origin TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE mb_cache (
    url TEXT PRIMARY KEY NOT NULL,
    body TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
