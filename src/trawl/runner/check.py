import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import MbDataError, Track
from trawl.net.client import HttpError
from trawl.runner.tools import AudioTools
from trawl.sources.base import Fetched
from trawl.sources.score import within_duration
from trawl.verify.fingerprint import AcoustId, AcoustResult
from trawl.verify.policy import Decision, decide, recording_ids
from trawl.verify.probe import AudioError, Probe


@dataclass(frozen=True)
class Checked:
    decision: Decision
    mismatch: bool = False
    comparison: str = ""

    def with_folder_guard(self, mismatch: bool) -> Decision:
        if mismatch and self.decision.verified_by == "duration":
            return Decision(
                False,
                None,
                "another file in this candidate was rejected by AcoustID",
                "rejected_verify",
            )
        return self.decision


@dataclass(frozen=True)
class Evidence:
    audio: Probe | None
    hits: tuple[AcoustResult, ...] = ()
    error: Checked | None = None

    def comparison(self) -> str:
        if not self.hits:
            return "AcoustID returned no recording matches"
        return "; ".join(
            f"{', '.join(hit.recording_ids) or 'no recording ID'} ({hit.score:.3f})"
            for hit in self.hits
        )


async def check(
    track: Track,
    path: Path,
    reliable: bool,
    acoustid: AcoustId,
    mb: MusicBrainzClient,
    min_score: float,
    tools: AudioTools,
) -> Checked:
    evidence = await inspect(path, acoustid, tools, expected=track)
    return await evaluate(track, evidence, reliable, mb, min_score)


async def inspect(
    path: Path, acoustid: AcoustId, tools: AudioTools, *, expected: Track | None = None
) -> Evidence:
    audio = None
    try:
        audio = await asyncio.to_thread(tools.probe, path)
        if expected and not within_duration(expected.length_ms, audio.duration_ms):
            return Evidence(
                audio,
                error=Checked(
                    Decision(False, None, "audio duration outside tolerance", "rejected_probe")
                ),
            )
        if not await asyncio.to_thread(tools.decode_ok, path):
            return Evidence(
                audio,
                error=Checked(
                    Decision(False, None, "audio could not be decoded", "rejected_probe")
                ),
            )
        length = expected.length_ms if expected else audio.duration_ms
        if length is not None and length < 30_000:
            return Evidence(audio)
        duration, fp = await asyncio.to_thread(tools.fingerprint, path)
    except (AudioError, OSError) as exc:
        return Evidence(audio, error=Checked(Decision(False, None, str(exc), "rejected_probe")))
    try:
        return Evidence(audio, tuple(await acoustid.lookup(duration, fp)))
    except (HttpError, MbDataError) as exc:
        return Evidence(
            audio,
            error=Checked(
                Decision(False, None, str(exc), "rejected_verify"),
                True,
                "AcoustID lookup did not complete",
            ),
        )


async def evaluate(
    track: Track, evidence: Evidence, reliable: bool, mb: MusicBrainzClient, min_score: float
) -> Checked:
    if evidence.error:
        return evidence.error
    audio, hits = evidence.audio, evidence.hits
    if audio is not None and not within_duration(track.length_ms, audio.duration_ms):
        return Checked(Decision(False, None, "audio duration outside tolerance", "rejected_probe"))
    try:
        ids = recording_ids(hits, min_score)
        same: set[str] = set()
        if (
            ids
            and (track.length_ms is None or track.length_ms >= 30_000)
            and track.recording_mbid not in ids
        ):
            planned = await mb.recording(track.recording_mbid)
            isrcs = set((*track.isrcs, *planned.isrcs))
            for mbid in ids:
                try:
                    match = await mb.recording(mbid)
                except HttpError as exc:
                    if exc.status != 404:
                        raise
                    continue
                if match.mbid == planned.mbid or isrcs.intersection(match.isrcs):
                    same.add(mbid)
                    break
        result = decide(track, audio, hits, reliable, False, same.__contains__, min_score=min_score)
        return Checked(result, bool(ids) and not result.accept, evidence.comparison())
    except (HttpError, MbDataError) as exc:
        return Checked(
            Decision(False, None, str(exc), "rejected_verify"), True, evidence.comparison()
        )


async def check_folder(
    tracks: Mapping[str, Track],
    files: Mapping[str, Fetched],
    reliable: bool,
    acoustid: AcoustId,
    mb: MusicBrainzClient,
    min_score: float,
    tools: AudioTools,
) -> dict[str, tuple[Fetched, Checked]]:
    evidence = {key: await inspect(file.path, acoustid, tools) for key, file in files.items()}
    claimed: set[str] = set()
    results: dict[str, tuple[Fetched, Checked]] = {}
    for key, item in evidence.items():
        ids = recording_ids(item.hits, min_score)
        matches = [
            (dest, track)
            for dest, track in tracks.items()
            if dest not in results
            and track.recording_mbid in ids
            and item.audio
            and within_duration(track.length_ms, item.audio.duration_ms)
        ]
        if len(matches) > 1:
            matches = [match for match in matches if match[0] == key]
        if not matches and ids:
            for dest, track in tracks.items():
                if (
                    dest not in results
                    and item.audio
                    and within_duration(track.length_ms, item.audio.duration_ms)
                ):
                    checked = await evaluate(track, item, reliable, mb, min_score)
                    if checked.decision.verified_by == "acoustid":
                        matches.append((dest, track))
        if len(matches) == 1:
            dest, track = matches[0]
            if dest not in results:
                checked = await evaluate(track, item, reliable, mb, min_score)
                if checked.decision.verified_by == "acoustid":
                    claimed.add(key)
                    results[dest] = files[key], checked
    displaced: list[str] = []
    for key, file in files.items():
        if key in claimed:
            continue
        if key in results:
            displaced.append(key)
        else:
            checked = await evaluate(tracks[key], evidence[key], reliable, mb, min_score)
            results[key] = file, checked
    remaining = [key for key in tracks if key not in results]
    for key, dest in zip(displaced, remaining, strict=False):
        item = evidence[key]
        checked = await evaluate(tracks[dest], item, False, mb, min_score)
        results[dest] = files[key], checked
    mismatch = any(checked.decision.outcome == "rejected_verify" for _, checked in results.values())
    if mismatch or any(
        (item.error is not None and item.error.decision.outcome == "rejected_verify")
        or recording_ids(item.hits, min_score)
        for item in (evidence[key] for key in displaced[len(remaining) :])
    ):
        return {
            key: (file, Checked(checked.with_folder_guard(True), checked.mismatch))
            for key, (file, checked) in results.items()
        }
    return results
