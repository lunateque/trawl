import shutil
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryFile
from typing import Literal

from trawl.config import Config, ConfigError, load, load_secrets
from trawl.db.connect import open_db
from trawl.library.place import staging_dir
from trawl.mb.client import MusicBrainzClient
from trawl.net.client import HttpClient, HttpError
from trawl.net.limiter import RateLimiter
from trawl.sources.base import FetchError, SourceWall
from trawl.sources.soulseek import SoulseekSource
from trawl.tag.replaygain import supports_opus
from trawl.verify.probe import AudioError


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
        return Check(name, "FAIL", "not found on PATH")
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


def _opus() -> Check:
    try:
        if supports_opus():
            return Check("rsgain Opus mode", "OK", "--opus-mode supported")
    except (AudioError, OSError) as exc:
        return Check("rsgain Opus mode", "FAIL", str(exc))
    return Check("rsgain Opus mode", "FAIL", "rsgain custom must support --opus-mode")


def _secrets(config: Path) -> Check:
    try:
        if load_secrets(config).acoustid_key:
            return Check("AcoustID", "OK", "application key configured")
    except ConfigError as exc:
        return Check("AcoustID", "FAIL", str(exc))
    return Check("AcoustID", "FAIL", "configure acoustid_key in secrets.toml")


def _staging(library: Path) -> Check:
    try:
        return _directory("Staging (exclude from player scans)", staging_dir(library))
    except OSError as exc:
        return Check("Staging", "FAIL", str(exc))


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


def _existing_directory(name: str, path: Path) -> Check:
    return (
        Check(name, "OK", str(path))
        if path.is_dir()
        else Check(name, "FAIL", f"Not a directory: {path}")
    )


async def _slskd(config: Path, download: Path, offline: bool) -> Check:
    try:
        secrets = load_secrets(config)
    except ConfigError as exc:
        return Check("slskd", "FAIL", str(exc))
    if not secrets.slskd_api_key:
        return Check("slskd", "FAIL", "configure slskd_api_key in secrets.toml")
    if offline:
        return Check("slskd", "SKIPPED", "offline")
    http = HttpClient("trawl doctor", {}, max_attempts=1)
    try:
        await SoulseekSource(http, secrets.slskd_url, secrets.slskd_api_key, download).check()
    except (FetchError, SourceWall) as exc:
        return Check("slskd", "FAIL", str(exc))
    finally:
        await http.aclose()
    return Check("slskd", "OK", "reachable and authorized")


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
        _opus(),
        _secrets(config),
        _staging(cfg.library_root),
    ]
    checks.append(
        Check("MusicBrainz", "SKIPPED", "offline") if offline else await _musicbrainz(cfg)
    )
    checks.extend(_existing_directory("Local inbox", path) for path in cfg.sources.local.inbox)
    if (
        "soulseek" in cfg.sources.order
        and (download := cfg.sources.soulseek.download_dir) is not None
    ):
        checks.append(_existing_directory("slskd download directory", download))
        checks.append(await _slskd(config, download, offline))
    return checks
