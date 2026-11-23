from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from trawl.tag.mapping import TagFields
from trawl.tag.replaygain import album_gain, track_gain
from trawl.tag.writer import write
from trawl.verify.fingerprint import fingerprint
from trawl.verify.probe import Probe, decode_ok, probe


@dataclass(frozen=True)
class AudioTools:
    probe: Callable[[Path], Probe] = probe
    decode_ok: Callable[[Path], bool] = decode_ok
    fingerprint: Callable[[Path], tuple[int, str]] = fingerprint
    write: Callable[[Path, TagFields, bytes | None, str | None], None] = write
    track_gain: Callable[[Path], None] = track_gain
    album_gain: Callable[[Sequence[Path]], None] = album_gain
