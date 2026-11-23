ALTER TABLE releases ADD COLUMN album_gain_done INTEGER NOT NULL DEFAULT 0;
ALTER TABLE releases ADD COLUMN album_gain_next_try_at TEXT;
