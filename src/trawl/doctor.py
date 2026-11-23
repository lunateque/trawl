import shutil
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryFile
from typing import Literal

from trawl.config import Config, ConfigError, load
from trawl.db.connect import open_db
from trawl.mb.client import MusicBrainzClient
from trawl.net.client import HttpClient, HttpError
from trawl.net.limiter import RateLimiter


@dataclass(frozen=True)
class Check:
    name: str
    status: Literal["OK", "WARNING", "FAIL", "SKIPPED"]
    detail: str


def _directory(name: str, path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with TemporaryFile(dir=path) as temp:
            temp.write(b"trawl doctor\n")
        return Check(name, "OK", str(path))
    except OSError as exc:
        return Check(name, "FAIL", str(exc))


def _database(path: Path) -> Check:
    try:
        with closing(open_db(path)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        return Check("Database", "OK", f"schema {version}")
    except (OSError, sqlite3.Error, ValueError) as exc:
        return Check("Database", "FAIL", str(exc))


def _binary(name: str, flag: str) -> Check:
    executable = shutil.which(name)
    if executable is None:
        return Check(name, "WARNING", "not found on PATH")
    try:
        # Executables and arguments are selected from a fixed list, not configuration.
        result = subprocess.run(  # noqa: S603
            [executable, flag],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(name, "FAIL", str(exc))
    lines = (result.stdout.strip() or result.stderr.strip()).splitlines()
    if not lines:
        return Check(name, "FAIL", "version command returned no output")
    return Check(name, "OK", lines[0])


async def _musicbrainz(cfg: Config) -> Check:
    from trawl import __version__

    http = HttpClient(
        f"trawl/{__version__} ( {cfg.contact_email} )",
        {"musicbrainz.org": RateLimiter(1.0)},
        max_attempts=1,
    )
    try:
        await http.get_json(
            f"{MusicBrainzClient.BASE}/artist", {"query": "test", "limit": "1", "fmt": "json"}
        )
    except HttpError as exc:
        return Check("MusicBrainz", "FAIL", str(exc))
    finally:
        await http.aclose()
    return Check("MusicBrainz", "OK", "reachable")


async def check(config: Path, offline: bool) -> list[Check]:
    try:
        cfg = load(config)
    except ConfigError as exc:
        return [Check("Config", "FAIL", str(exc))]
    checks = [
        Check("Config", "OK", str(config)),
        _directory("Library root", cfg.library_root),
        _directory("Data directory", cfg.data_dir),
        _database(cfg.data_dir / "trawl.sqlite"),
        *(
            _binary(name, flag)
            for name, flag in (
                ("ffmpeg", "-version"),
                ("ffprobe", "-version"),
                ("fpcalc", "-version"),
                ("rsgain", "--version"),
            )
        ),
    ]
    checks.append(
        Check("MusicBrainz", "SKIPPED", "offline") if offline else await _musicbrainz(cfg)
    )
    return checks
