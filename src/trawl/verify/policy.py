from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from trawl.core.audio import Probe, within_duration
from trawl.core.catalog import Track
from trawl.verify.fingerprint import AcoustResult


@dataclass(frozen=True)
class Decision:
    accept: bool
    verified_by: Literal["acoustid", "duration"] | None
    reason: str
    outcome: Literal["accepted", "rejected_probe", "rejected_verify"]


def recording_ids(results: Sequence[AcoustResult], min_score: float) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            mbid for result in results if result.score >= min_score for mbid in result.recording_ids
        )
    )


def decide(
    planned: Track,
    probe: Probe | None,
    acoustid_results: Sequence[AcoustResult],
    candidate_reliable: bool,
    folder_has_acoustid_reject: bool,
    resolve_same: Callable[[str], bool],
    *,
    min_score: float = 0.8,
) -> Decision:
    if probe is None:
        return Decision(False, None, "audio could not be decoded", "rejected_probe")
    if not within_duration(planned.length_ms, probe.duration_ms):
        return Decision(False, None, "audio duration outside tolerance", "rejected_probe")
    ids = recording_ids(acoustid_results, min_score)
    if planned.length_ms is None or planned.length_ms >= 30_000:
        if planned.recording_mbid in ids or any(resolve_same(mbid) for mbid in ids):
            return Decision(True, "acoustid", "recording matched by AcoustID", "accepted")
        if ids:
            return Decision(
                False, None, "AcoustID matched a different recording", "rejected_verify"
            )
    if planned.length_ms is None:
        reason = "unknown planned duration requires AcoustID"
    elif folder_has_acoustid_reject:
        reason = "another file in this candidate was rejected by AcoustID"
    elif not candidate_reliable:
        reason = "duration alone requires a reliable album candidate"
    else:
        return Decision(True, "duration", "duration matched in reliable album", "accepted")
    return Decision(False, None, reason, "rejected_verify")
