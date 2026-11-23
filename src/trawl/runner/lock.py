import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class LockBusy(OSError):  # noqa: N818 - distinct from lockfile I/O errors
    def __init__(self, pid: str) -> None:
        self.pid = pid
        super().__init__(f"another trawl is running (pid {pid})")


@contextmanager
def acquire(data_dir: Path) -> Generator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(data_dir / "trawl.lock", os.O_CREAT | os.O_RDWR)
    with os.fdopen(fd, "r+b", buffering=0) as lock:
        try:
            if sys.platform == "win32":
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if not isinstance(exc, BlockingIOError) and exc.errno not in {13, 11}:
                raise
            lock.seek(1)
            owner = (lock.read() or b"").strip().decode("ascii", errors="replace")
            raise LockBusy(owner if owner.isdecimal() else "unknown") from exc
        lock.seek(1)
        lock.write(f"{os.getpid()}\n".encode("ascii"))
        lock.truncate()
        yield
