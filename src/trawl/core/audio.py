from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from trawl.core.tags import TagFields

TAGGABLE = frozenset({".flac", ".opus", ".ogg", ".m4a", ".mp3"})
REPACK_EXTS = frozenset({".wav", ".aiff", ".aif", ".ape", ".wv"})
AUDIO_EXTS = TAGGABLE | REPACK_EXTS
LOSSLESS_EXTS = REPACK_EXTS | {".flac"}

LOSSLESS = frozenset({"flac", "alac", "ape", "wavpack"})


def within_duration(expected: int | None, actual: int | None) -> bool:
    return expected is None or (
        actual is not None and abs(expected - actual) <= max(3000, expected * 0.02)
    )


@dataclass(frozen=True)
class Probe:
    codec: str
    sample_rate: int
    channels: int
    duration_ms: int
    bitrate: int | None
    bits: int | None = None
    sample_fmt: str | None = None


@dataclass(frozen=True)
class AudioTools:
    probe: Callable[[Path], Probe]
    decode_ok: Callable[[Path], bool]
    fingerprint: Callable[[Path], tuple[int, str]]
    write: Callable[[Path, TagFields, bytes | None, str | None], None]
    track_gain: Callable[[Path], None]
    album_gain: Callable[[Sequence[Path]], None]
