import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from trawl.mb.models import array_value, object_value

LOSSLESS = frozenset({"flac", "alac", "ape", "wavpack"})


class AudioError(Exception):
    pass


def command(binary: str, *args: str) -> subprocess.CompletedProcess[str]:
    if not (executable := shutil.which(binary)):
        raise AudioError(f"{binary} not found on PATH")
    return subprocess.run(  # noqa: S603
        [executable, *args], capture_output=True, encoding="utf-8", errors="replace", check=False
    )


@dataclass(frozen=True)
class Probe:
    codec: str
    sample_rate: int
    channels: int
    duration_ms: int
    bitrate: int | None


def probe(path: Path) -> Probe:
    result = command(
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    )
    if result.returncode or result.stderr.strip():
        raise AudioError(result.stderr.strip() or "ffprobe failed")
    try:
        data = object_value(json.loads(result.stdout))
        audio = next(
            stream
            for item in array_value(data.get("streams"))
            if (stream := object_value(item)).get("codec_type") == "audio"
        )
        info = object_value(data.get("format"))
        bitrate = audio.get("bit_rate") or info.get("bit_rate")
        return Probe(
            str(audio["codec_name"]),
            int(str(audio["sample_rate"])),
            int(str(audio["channels"])),
            round(1000 * float(str(audio.get("duration") or info["duration"]))),
            round(int(str(bitrate)) / 1000) if str(bitrate).isdigit() else None,
        )
    except (KeyError, ValueError, StopIteration, OverflowError) as exc:
        raise AudioError("ffprobe did not report a readable audio stream") from exc


def decode_ok(path: Path) -> bool:
    result = command(
        "ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-f", "null", "-"
    )
    return result.returncode == 0 and not result.stderr
