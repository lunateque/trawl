import ctypes
import logging
import sqlite3
import stat
import sys
from pathlib import Path

from trawl.core.paths import is_link, is_link_stat

log = logging.getLogger(__name__)


def _require_unlinked_staging(root: Path) -> None:
    if is_link(root):
        raise OSError(f"Staging directory must not be a symlink or junction: {root}")


def staging_dir(library: Path) -> Path:
    root = library.resolve() / ".trawl-staging"
    _require_unlinked_staging(root)
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
    _require_unlinked_staging(root)
    keep: set[Path] = set()
    for (raw,) in conn.execute(
        """SELECT path FROM tracks WHERE state IN ('fetched','verified','tagged')
        AND path IS NOT NULL
        UNION ALL
        SELECT r.path FROM current_rejections r JOIN tracks t ON t.id=r.track_id
        WHERE t.state IN ('searching','missing')"""
    ):
        path = Path(raw).absolute()
        keep.update((path, path.with_suffix(".lrc"), path.parent / "cover.jpg"))

    def clean(directory: Path) -> None:
        for path in directory.iterdir():
            try:
                info = path.lstat()
                if is_link_stat(info):
                    log.warning("Skipping linked path %s", path)
                elif stat.S_ISREG(info.st_mode) and path not in keep:
                    path.unlink()
                elif stat.S_ISDIR(info.st_mode):
                    clean(path)
                    if not any(path.iterdir()):
                        path.rmdir()
            except OSError as exc:
                log.warning("Cannot remove stale staging path %s: %s", path, exc)

    clean(root)
