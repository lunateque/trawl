ALTER TABLE unresolved ADD COLUMN target_kind TEXT NOT NULL DEFAULT 'recording'
    CHECK (target_kind IN ('recording', 'release_group', 'artist'));
DELETE FROM unresolved WHERE id NOT IN (SELECT MAX(id) FROM unresolved GROUP BY origin, origin_ref);
CREATE UNIQUE INDEX unresolved_origin_ref ON unresolved(origin, origin_ref);
CREATE TABLE resolved_refs (
    origin TEXT NOT NULL CHECK (origin IN ('listenbrainz', 'yandex', 'spotify', 'manual', 'releases')),
    origin_ref TEXT NOT NULL,
    PRIMARY KEY (origin, origin_ref)
);
CREATE TABLE artist_release_plays (
    artist_mbid TEXT NOT NULL,
    release_group_mbid TEXT NOT NULL,
    plays INTEGER NOT NULL CHECK (plays > 0),
    PRIMARY KEY (artist_mbid, release_group_mbid)
);
ALTER TABLE artists ADD COLUMN follow_override INTEGER CHECK (follow_override IN (0, 1));
