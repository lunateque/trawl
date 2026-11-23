import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from trawl.net.limiter import RateLimiter


class HttpError(Exception):
    def __init__(self, url: str, status: int | None, detail: str) -> None:
        self.url = url
        self.status = status
        super().__init__(f"{url}: {detail}")


def _retry_after(header: str) -> float | None:
    if header.isascii() and header.isdecimal():
        seconds = header.lstrip("0")
        return min(300, int(seconds or "0")) if len(seconds) <= 3 else 300
    try:
        when = parsedate_to_datetime(header)
    except (ValueError, OverflowError):
        return None
    remaining = when.replace(tzinfo=when.tzinfo or UTC) - datetime.now(UTC)
    return min(300, max(0, remaining.total_seconds()))


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        limiters: Mapping[str, RateLimiter],
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
        max_attempts: int = 5,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._limiters = limiters
        self._sleep = sleep
        self._rand = rand
        self._max_attempts = max_attempts
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            timeout=30,
            transport=transport,
            event_hooks={"request": [self._limit]},
        )

    async def _limit(self, request: httpx.Request) -> None:
        if limiter := self._limiters.get(request.url.host):
            async with limiter:
                pass

    async def get_json(self, url: str, params: Mapping[str, str] | None = None) -> Any:
        last: HttpError
        for attempt in range(self._max_attempts):
            delay = None
            try:
                response = await self._client.get(url, params=params)
            except httpx.TransportError as exc:
                last = HttpError(url, None, str(exc))
            else:
                final_url = str(response.request.url)
                code = response.status_code
                if response.is_success:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise HttpError(final_url, code, "invalid JSON response") from exc
                last = HttpError(final_url, code, f"HTTP {code}")
                if code != 429 and not response.is_server_error:
                    raise last
                if code in {429, 503} and (header := response.headers.get("Retry-After")):
                    delay = _retry_after(header)
            if attempt + 1 == self._max_attempts:
                raise last
            if delay is None:
                delay = min(60, 2**attempt) * (0.5 + self._rand())
            await self._sleep(delay)

    async def aclose(self) -> None:
        await self._client.aclose()
