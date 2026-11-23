import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic


class RateLimiter:
    def __init__(
        self,
        min_interval: float,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._next = float("-inf")
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> None:
        async with self._lock:
            if (delay := self._next - self._clock()) > 0:
                await self._sleep(delay)
            self._next = self._clock() + self._interval

    async def __aexit__(self, *_: object) -> None:
        pass
