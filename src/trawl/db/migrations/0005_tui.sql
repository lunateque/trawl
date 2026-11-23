ALTER TABLE attempts ADD COLUMN candidate_key TEXT;
CREATE INDEX attempts_track_recent ON attempts(track_id, id DESC);
CREATE INDEX attempts_recent ON attempts(at);

CREATE TABLE review_files (
    attempt_id INTEGER PRIMARY KEY REFERENCES attempts(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    codec TEXT NOT NULL,
    bitrate INTEGER,
    lossless INTEGER NOT NULL,
    comparison TEXT NOT NULL
);
CREATE VIEW current_rejections AS
    SELECT a.id AS attempt_id, a.track_id, a.source, a.reason,
           f.path, f.codec, f.bitrate, f.lossless, f.comparison
    FROM review_files f JOIN attempts a ON a.id = f.attempt_id
    WHERE a.id = (SELECT max(newer.id) FROM attempts newer WHERE newer.track_id = a.track_id);

CREATE TABLE search_candidates (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    ref TEXT NOT NULL,
    item_ref TEXT NOT NULL,
    size INTEGER,
    title TEXT NOT NULL,
    score REAL NOT NULL,
    after_attempt INTEGER NOT NULL,
    PRIMARY KEY (track_id, source, ref, item_ref)
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    state TEXT,
    attempt_id INTEGER REFERENCES attempts(id) ON DELETE CASCADE,
    codec TEXT,
    verified_by TEXT
);
CREATE INDEX events_track_recent ON events(track_id, id DESC) WHERE state IS NOT NULL;
CREATE INDEX events_recent ON events(at DESC, id DESC);

CREATE TRIGGER events_track_insert AFTER INSERT ON tracks BEGIN
    INSERT INTO events(track_id, at, state, codec, verified_by)
    VALUES (NEW.id, strftime('%Y-%m-%dT%H:%M:%f000+00:00','now'),
            NEW.state, NEW.codec, NEW.verified_by);
END;
CREATE TRIGGER events_track_update AFTER UPDATE OF state ON tracks BEGIN
    INSERT INTO events(track_id, at, state, codec, verified_by)
    VALUES (NEW.id, strftime('%Y-%m-%dT%H:%M:%f000+00:00','now'),
            NEW.state, NEW.codec, NEW.verified_by);
END;
CREATE TRIGGER events_attempt AFTER INSERT ON attempts BEGIN
    DELETE FROM review_files WHERE attempt_id = (
        SELECT max(id) FROM attempts WHERE track_id = NEW.track_id AND id < NEW.id
    );
    INSERT INTO events(track_id, at, attempt_id) VALUES (NEW.track_id, NEW.at, NEW.id);
END;

INSERT INTO events(track_id, at, state, codec, verified_by)
SELECT id, strftime('%Y-%m-%dT%H:%M:%f000+00:00','now'), state, codec, verified_by FROM tracks;
INSERT INTO events(track_id, at, attempt_id)
SELECT track_id, at, id FROM (SELECT * FROM attempts ORDER BY id DESC LIMIT 200) ORDER BY id;
