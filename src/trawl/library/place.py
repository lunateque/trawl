import ctypes
import logging
import sqlite3
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def staging_dir(library: Path) -> Path:
    root = library.resolve() / ".trawl-staging"
    if not root.is_dir():
        root.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            attrs = kernel.GetFileAttributesW(str(root))
            if attrs == -1 or not kernel.SetFileAttributesW(str(root), attrs | 2):
                raise ctypes.WinError(ctypes.get_last_error())
    return root


def place(staged: Path, final: Path) -> bool:
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(final)
    except PermissionError:
        return False
    return True


def cleanup_staging(conn: sqlite3.Connection, root: Path) -> None:
    keep: set[Path] = set()
    for (raw,) in conn.execute(
        "SELECT path FROM tracks WHERE state IN ('fetched','verified','tagged') "
        "AND path IS NOT NULL"
    ):
        path = Path(raw).resolve()
        keep.update((path, path.with_suffix(".lrc"), path.parent / "cover.jpg"))
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            if path.is_symlink() or path.is_file():
                if path not in keep:
                    path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError as exc:
            log.warning("Cannot remove stale staging path %s: %s", path, exc)
