import asyncio
from dataclasses import dataclass
from pathlib import Path

from trawl.mb.client import MusicBrainzClient
from trawl.mb.models import MbDataError, Track
from trawl.net.client import HttpError
from trawl.runner.tools import AudioTools
from trawl.sources.score import within_duration
from trawl.verify.fingerprint import AcoustId
from trawl.verify.policy import Decision, decide, recording_ids
from trawl.verify.probe import AudioError


@dataclass(frozen=True)
class Checked:
    decision: Decision
    mismatch: bool = False

    def with_folder_guard(self, mismatch: bool) -> Decision:
        if mismatch and self.decision.verified_by == "duration":
            return Decision(
                False,
                None,
                "another file in this candidate was rejected by AcoustID",
                "rejected_verify",
            )
        return self.decision


async def check(
    track: Track,
    path: Path,
    reliable: bool,
    acoustid: AcoustId,
    mb: MusicBrainzClient,
    min_score: float,
    tools: AudioTools,
) -> Checked:
    try:
        audio = await asyncio.to_thread(tools.probe, path)
        if not within_duration(track.length_ms, audio.duration_ms):
            return Checked(
                Decision(False, None, "audio duration outside tolerance", "rejected_probe")
            )
        if not await asyncio.to_thread(tools.decode_ok, path):
            return Checked(Decision(False, None, "audio could not be decoded", "rejected_probe"))
        if track.length_ms is not None and track.length_ms < 30_000:
            return Checked(
                decide(track, audio, (), reliable, False, lambda _: False, min_score=min_score)
            )
        duration, fp = await asyncio.to_thread(tools.fingerprint, path)
    except (AudioError, OSError) as exc:
        return Checked(Decision(False, None, str(exc), "rejected_probe"))
    try:
        hits = await acoustid.lookup(duration, fp)
        ids = recording_ids(hits, min_score)
        same: set[str] = set()
        if ids and track.recording_mbid not in ids:
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
        return Checked(result, bool(ids) and not result.accept)
    except (HttpError, MbDataError) as exc:
        return Checked(Decision(False, None, str(exc), "rejected_verify"))
