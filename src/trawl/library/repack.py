import re
from pathlib import Path

from trawl.sources.folder import REPACK_EXTS
from trawl.verify.probe import AudioError, Probe, command, probe

_INTEGER_PCM = re.compile(r"pcm_[su](?:8|16|24|32)(?:le|be)?")


def repack_needed(path: Path) -> bool:
    return path.suffix.casefold() in REPACK_EXTS


def _bits(audio: Probe) -> int:
    if audio.bits is not None:
        return audio.bits
    if audio.codec == "wavpack":
        return 32 if audio.sample_fmt == "s32p" else 16
    return 0


def _require_lossless(src: Path, audio: Probe) -> None:
    if audio.codec.startswith("pcm_f") or audio.sample_fmt in {"flt", "fltp", "dbl", "dblp"}:
        raise AudioError("float pcm")
    if audio.codec == "wavpack":
        # FFmpeg ignores .wvc. Its bitrate alone cannot distinguish hybrid from lossless.
        with src.open("rb") as stream:
            header = stream.read(32)
        if len(header) != 32 or not header.startswith(b"wvpk"):
            raise AudioError("invalid wavpack header")
        flags = int.from_bytes(header[24:28], "little")
        if flags & 0x8:
            raise AudioError("hybrid wavpack")
    elif audio.codec != "ape" and not _INTEGER_PCM.fullmatch(audio.codec):
        raise AudioError(f"cannot repack {audio.codec} losslessly")
    if _bits(audio) > 32:
        raise AudioError("audio has more than 32 bits per sample")


def _ffmpeg(src: Path, *args: str) -> str:
    result = command("ffmpeg", "-v", "error", "-nostdin", "-i", str(src), "-map", "0:a:0", *args)
    if result.returncode or result.stderr:
        raise AudioError("ffmpeg repack failed")
    return result.stdout.strip()


def repack(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        raise AudioError(f"repack destination already exists: {dst}")
    audio = probe(src)
    _require_lossless(src, audio)
    pcm = ("-c:a", "pcm_s32le", "-f", "md5", "-")
    expected = _ffmpeg(src, *pcm)
    # A fixed 32-bit PCM representation keeps the checksum sensitive to >16-bit changes.
    depth = ("-sample_fmt", "s32", "-strict", "experimental") if _bits(audio) > 16 else ()
    try:
        _ffmpeg(src, *depth, "-c:a", "flac", "-compression_level", "8", "-n", str(dst))
        if not expected.startswith("MD5=") or expected != _ffmpeg(dst, *pcm):
            raise AudioError("lossless repack PCM checksum mismatch")
    except AudioError:
        dst.unlink(missing_ok=True)
        raise
