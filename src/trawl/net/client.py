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
        return self._json(await self._request("GET", url, params=params))

    async def post_form(self, url: str, data: Mapping[str, str]) -> Any:
        return self._json(await self._request("POST", url, data=data))

    async def api_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: object = None,
        params: Mapping[str, str] | None = None,
    ) -> object:
        response = await self._request(
            method, url, params=params, json=body, request_headers=headers
        )
        return self._json(response) if response.content else None

    async def get_bytes(self, url: str) -> bytes:
        response = await self._request("GET", url, accept_encoding="identity")
        length = response.headers.get("Content-Length")
        if length is not None and length != str(len(response.content)):
            raise HttpError(str(response.url), response.status_code, "Content-Length mismatch")
        return response.content

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise HttpError(
                str(response.url), response.status_code, "invalid JSON response"
            ) from exc

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        accept_encoding: str | None = None,
        json: object = None,
        request_headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        last = HttpError(url, None, "no HTTP attempts configured")
        for attempt in range(self._max_attempts):
            delay = None
            try:
                headers = (
                    {"Accept-Encoding": accept_encoding} if accept_encoding else request_headers
                )
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=headers,
                    json=json,
                    follow_redirects=request_headers is None,
                )
            except httpx.TransportError as exc:
                last = HttpError(url, None, str(exc))
            else:
                final_url = str(response.request.url)
                code = response.status_code
                if response.is_success:
                    return response
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
        raise last

    async def aclose(self) -> None:
        await self._client.aclose()
