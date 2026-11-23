ALTER TABLE releases ADD COLUMN upgrade_checked_at TEXT;
ALTER TABLE releases ADD COLUMN upgrade_next_try_at TEXT;
ALTER TABLE releases ADD COLUMN switched_to TEXT REFERENCES releases(mbid);

CREATE TABLE import_folders (
    want_id INTEGER PRIMARY KEY REFERENCES wants(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    mapping TEXT NOT NULL
);
