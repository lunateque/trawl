import asyncio
import json
import sqlite3
from collections import Counter
from pathlib import Path

from trawl.config import Config
from trawl.db import repo
from trawl.db.repo import Kind, Origin, Scope, Want, WantStatus
from trawl.db.states import Actor, TrackState, transition
from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import Release, object_value
from trawl.net.client import HttpError
from trawl.plan.choose import choose_release
from trawl.plan.run import store_tracks
from trawl.sources.base import Candidate, CandidateItem
from trawl.sources.folder import (
    FolderFile,
    FolderMatch,
    group_disc_subfolders,
    match_folder,
    pick_format_set,
    reject_cue_image,
)
from trawl.sources.local import scan_folder
from trawl.sources.score import release_tracks
from trawl.verify.fingerprint import AcoustId, fingerprint
from trawl.verify.policy import recording_ids
from trawl.verify.probe import AudioError


def _match_recordings(
    release: Release, files: list[FolderFile], identities: dict[FolderFile, tuple[str, ...]]
) -> dict[str, FolderFile]:
    tracks = release_tracks(release)
    # Prefer the numbered/title mapping when a recording occurs twice.
    provisional = match_folder(release, tracks, files)
    options_by_file = {
        file: sorted(
            (track.mbid for track in tracks if track.recording_mbid in ids),
            key=lambda key: provisional.get(key) != file,
        )
        for file, ids in identities.items()
    }
    matched: dict[str, FolderFile] = {}

    def assign(file: FolderFile, seen: set[str]) -> bool:
        for key in options_by_file[file]:
            if key not in seen:
                seen.add(key)
                if key not in matched or assign(matched[key], seen):
                    matched[key] = file
                    return True
        return False

    for file in options_by_file:
        assign(file, set())
    return matched


async def plan_import(
    conn: sqlite3.Connection, cfg: Config, mb: MusicBrainzClient, acoustid: AcoustId, root: Path
) -> Release:
    root = await asyncio.to_thread(root.resolve, strict=True)
    files = [file for file, _ in await asyncio.to_thread(scan_folder, root)]
    if reject_cue_image(files):
        raise ValueError("Cannot import an audio image with a cue sheet")
    files = pick_format_set(group_disc_subfolders(files))
    if not files:
        raise ValueError("No audio files in the import folder")
    identities: dict[FolderFile, tuple[str, ...]] = {}
    canonical: dict[str, str | None] = {}
    for file in files:
        try:
            duration, fp = await asyncio.to_thread(fingerprint, root / file.rel_path)
        except AudioError:
            continue
        matches: list[str] = []
        for mbid in recording_ids(await acoustid.lookup(duration, fp), cfg.acoustid_min_score):
            if mbid not in canonical:
                try:
                    canonical[mbid] = (await mb.recording(mbid)).mbid
                except HttpError as exc:
                    if exc.status != 404:
                        raise
                    canonical[mbid] = None
            if recording := canonical[mbid]:
                matches.append(recording)
        identities[file] = tuple(dict.fromkeys(matches))
    appearances: Counter[str] = Counter()
    related: dict[str, set[str]] = {}
    for ids in identities.values():
        for mbid in ids:
            if mbid not in related:
                related[mbid] = {release.mbid for release in await mb.recording_releases(mbid)}
        appearances.update({release for mbid in ids for release in related[mbid]})
    options: list[Release] = []
    mappings: dict[str, dict[str, FolderFile]] = {}
    for mbid, count in sorted(appearances.items()):
        if count * 5 < len(files) * 4:
            continue
        release = await mb.release(mbid)
        matched = _match_recordings(release, files, identities)
        if len(matched) * 5 >= len(files) * 4:
            options.append(release)
            mappings[mbid] = matched
    choice = choose_release(options, cfg.plan)
    if choice is None:
        raise ValueError("No MusicBrainz release matches at least 80% of the folder")
    release, reason = choice
    reason.update(selected_by="AcoustID folder import")
    mapping = mappings[release.mbid]
    remaining = [
        file for file in files if file not in mapping.values() and not identities.get(file)
    ]
    other_tracks = [track for track in release_tracks(release) if track.mbid not in mapping]
    mapping.update(match_folder(release, other_tracks, remaining))
    source = f"import:{root}"
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT id FROM wants WHERE kind='release' AND mbid=?
            AND origin='manual' AND origin_ref=? AND status!='dropped'""",
            (release.mbid, source),
        ).fetchone()
        if row:
            want_id = int(row[0])
        else:
            cursor = conn.execute(
                """INSERT INTO wants(kind,mbid,scope,origin,origin_ref,status,created_at)
                VALUES ('release',?,'album','manual',?,'new',datetime('now')) RETURNING id""",
                (release.mbid, source),
            )
            want_id = int(cursor.fetchone()[0])
        store_tracks(
            conn,
            Want(want_id, Kind.RELEASE, release.mbid, Scope.ALBUM, Origin.MANUAL, None),
            release,
            reason,
            None,
        )
        repo.set_want_status(conn, want_id, WantStatus.PLANNED)
        conn.execute(
            """INSERT INTO import_folders(want_id,path,mapping) VALUES (?,?,?)
            ON CONFLICT(want_id) DO UPDATE SET path=excluded.path,mapping=excluded.mapping""",
            (want_id, str(root), json.dumps({key: file.ref for key, file in mapping.items()})),
        )
        missing = conn.execute(
            """SELECT id FROM tracks WHERE release_mbid=? AND state='missing'
            AND release_track_mbid IN (SELECT value FROM json_each(?))""",
            (release.mbid, json.dumps(list(mapping))),
        ).fetchall()
        for (track_id,) in missing:
            transition(
                conn, track_id, TrackState.MISSING, TrackState.PLANNED, Actor.USER, next_try_at=None
            )
    return release


def imported_folder(conn: sqlite3.Connection, release: Release) -> list[FolderMatch]:
    matches: list[FolderMatch] = []
    tracks = release_tracks(release)
    for folder, raw in conn.execute(
        """SELECT i.path,i.mapping FROM import_folders i JOIN wants w ON w.id=i.want_id
        WHERE w.mbid=? AND w.status='planned' ORDER BY w.id DESC""",
        (release.mbid,),
    ):
        mapping = object_value(json.loads(raw))
        items = {
            track.mbid: CandidateItem(ref, track.position, track.length_ms, track.title)
            for track in tracks
            if isinstance(ref := mapping.get(track.mbid), str)
        }
        if items and Path(folder).is_dir():
            candidate = Candidate(
                "local",
                folder,
                "album",
                release.title,
                None,
                None,
                False,
                False,
                tuple(items.values()),
            )
            matches.append(FolderMatch(release, candidate, items, len(items) == len(tracks)))
    return matches
