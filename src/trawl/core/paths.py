import logging
import os
import stat
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

log = logging.getLogger("trawl.library.place")


def is_link_stat(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    if sys.platform == "win32":
        return info.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    return False


def is_link(path: Path) -> bool:
    try:
        return is_link_stat(path.lstat())
    except FileNotFoundError:
        return False


def linked_ancestor(path: Path) -> Path | None:
    for parent in reversed(path.absolute().parents):
        if is_link(parent):
            return parent
    return path if is_link(path) else None


def unlink_regular(path: Path) -> bool:
    if linked := linked_ancestor(path.parent):
        log.warning("Skipping linked path %s", linked)
        return False
    info = path.lstat()
    if is_link_stat(info):
        log.warning("Skipping linked path %s", path)
        return False
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"Not a regular file: {path}")
    path.unlink()
    return True


def write_atomic(path: Path, data: bytes) -> None:
    temp: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(data)
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
