import re
from collections.abc import Sequence
from pathlib import Path

from trawl.verify.probe import AudioError, command


def supports_opus() -> bool:
    result = command("rsgain", "custom", "--help")
    return result.returncode == 0 and bool(
        re.search(r"(?:--opus-mode\b|(?m:^\s*-o[ ,]))", result.stdout + result.stderr)
    )


def _gain(paths: Sequence[Path], *, album: bool) -> None:
    if not paths:
        return
    for path in paths:
        # rsgain can return success even when the player prevented a tag write.
        with path.open("r+b"):
            pass
    result = command(
        "rsgain",
        "custom",
        "-s",
        "i",
        "-o",
        "s",
        "-I",
        "4",
        "-q",
        *(["-a"] if album else []),
        "--",
        *(str(p.absolute()) for p in paths),
    )
    if result.returncode or "error" in result.stderr.casefold():
        raise AudioError(result.stderr.strip() or "ReplayGain scan failed")


def track_gain(path: Path) -> None:
    _gain([path], album=False)


def album_gain(paths: Sequence[Path]) -> None:
    _gain(paths, album=True)
