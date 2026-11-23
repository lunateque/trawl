import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import BinaryIO

from trawl.mb.models import array_value, object_value

LOSSLESS = frozenset({"flac", "alac", "ape", "wavpack"})
_OUTPUT_LIMIT = 1024 * 1024


class AudioError(Exception):
    pass


def command(
    binary: str, *args: str, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    if not (executable := shutil.which(binary)):
        raise AudioError(f"{binary} not found on PATH")
    seconds = timeout if timeout is not None else (600 if binary in {"ffmpeg", "rsgain"} else 60)
    with subprocess.Popen(  # noqa: S603
        [executable, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        if child.stdout is None or child.stderr is None:
            child.kill()
            raise AudioError(f"{binary} could not capture output")
        output, errors = bytearray(), bytearray()

        def drain(pipe: BinaryIO, captured: bytearray) -> None:
            while chunk := pipe.read(65536):
                left = _OUTPUT_LIMIT + 1 - len(captured)
                if left:
                    captured.extend(chunk[:left])

        readers = (
            Thread(target=drain, args=(child.stdout, output)),
            Thread(target=drain, args=(child.stderr, errors)),
        )
        for reader in readers:
            reader.start()
        try:
            status = child.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            raise AudioError(f"{binary} timed out after {seconds:g}s") from None
        finally:
            for reader in readers:
                reader.join()
        if len(output) > _OUTPUT_LIMIT or len(errors) > _OUTPUT_LIMIT:
            raise AudioError(f"{binary} output exceeds capture limit")
        return subprocess.CompletedProcess(
            child.args, status, output.decode("utf-8", "replace"), errors.decode("utf-8", "replace")
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
        raise AudioError("ffprobe failed to read the audio file")
    try:
        data = object_value(json.loads(result.stdout))
        audio = next(
            stream
            for item in array_value(data.get("streams"))
            if (stream := object_value(item)).get("codec_type") == "audio"
        )
        info = object_value(data.get("format"))
        bitrate = audio.get("bit_rate") or info.get("bit_rate")
        bits = audio.get("bits_per_raw_sample") or audio.get("bits_per_sample")
        sample_fmt = audio.get("sample_fmt")
        return Probe(
            str(audio["codec_name"]),
            int(str(audio["sample_rate"])),
            int(str(audio["channels"])),
            round(1000 * float(str(audio.get("duration") or info["duration"]))),
            round(int(str(bitrate)) / 1000) if str(bitrate).isdigit() else None,
            int(str(bits)) if str(bits).isdigit() and int(str(bits)) else None,
            sample_fmt if isinstance(sample_fmt, str) else None,
        )
    except (KeyError, ValueError, StopIteration, OverflowError, RecursionError) as exc:
        raise AudioError("ffprobe did not report a readable audio stream") from exc


def decode_ok(path: Path) -> bool:
    result = command(
        "ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-f", "null", "-"
    )
    return result.returncode == 0 and not result.stderr
