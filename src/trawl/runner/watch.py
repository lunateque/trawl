import asyncio
import logging
import signal
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable, Generator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from types import FrameType

from trawl.taste.store import cursor, set_cursor

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    name: str
    interval: int
    run: Callable[[], Awaitable[None]]


async def sleep_until_stop(stop: asyncio.Event, delay: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=delay)


@contextmanager
def graceful_stop(stop: asyncio.Event) -> Generator[None]:
    loop = asyncio.get_running_loop()
    signals = [signal.SIGINT, signal.SIGTERM]
    if sys.platform == "win32":
        signals.append(signal.SIGBREAK)
    installed: list[tuple[int, int | Callable[[int, FrameType | None], object] | None]] = []

    def handler(_sig: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(stop.set)

    try:
        for signum in signals:
            installed.append((signum, signal.signal(signum, handler)))
        yield
    finally:
        for signum, previous in installed:
            if previous is not None:
                signal.signal(signum, previous)


async def watch(
    conn: sqlite3.Connection,
    jobs: Sequence[Job],
    stop: asyncio.Event,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[asyncio.Event, float], Awaitable[None]] = sleep_until_stop,
) -> None:
    due: dict[str, float] = {}
    for job in jobs:
        last = cursor(conn, f"watch:{job.name}")
        due[job.name] = 0 if last is None else float(last) + job.interval
    while not stop.is_set():
        for job in jobs:
            if stop.is_set():
                return
            if due[job.name] > clock():
                continue
            try:
                await job.run()
            except Exception as exc:
                # No traceback: SDK exceptions can contain request credentials.
                _LOG.warning(
                    "watch.job_failed",
                    extra={"fields": {"job": job.name, "error": type(exc).__name__}},
                )
            when = clock()
            with conn:
                set_cursor(conn, f"watch:{job.name}", str(when))
            due[job.name] = when + job.interval
        if not stop.is_set():
            await sleep(stop, max(0.0, min(due.values()) - clock()))
