import asyncio
import json
import random
import zlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from io import BytesIO
from typing import Any

import httpx

from trawl.net.limiter import RateLimiter

_JSON_LIMIT = 10_000_000
_COVER_LIMIT = 20_000_000
_SERVICES = {
    "musicbrainz.org": "MusicBrainz",
    "api.acoustid.org": "AcoustID",
    "coverartarchive.org": "CAA",
    "lrclib.net": "LRCLIB",
    "api.listenbrainz.org": "ListenBrainz",
    "api.spotify.com": "Spotify",
    "accounts.spotify.com": "Spotify",
}


class HttpError(Exception):
    def __init__(
        self,
        url: str | httpx.URL,
        status: int | None,
        detail: str,
        *,
        service: str | None = None,
        kind: str = "HttpError",
    ) -> None:
        address = httpx.URL(url)
        self.url = str(address.copy_with(query=None, fragment=None, username=None, password=None))
        self.status = status
        name = service or _SERVICES.get(address.host, address.host)
        code = f"HTTP {status}" if status is not None else "no HTTP status"
        super().__init__(f"{name}: {code} {kind} {self.url}: {detail}")


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


async def _read(response: httpx.Response, limit: int, service: str) -> bytes:
    def error(detail: str, kind: str = "HttpError") -> HttpError:
        return HttpError(response.url, response.status_code, detail, service=service, kind=kind)

    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in {"gzip", "identity"}:
        raise error("unsupported Content-Encoding")
    length = response.headers.get("Content-Length") if encoding == "identity" else None
    expected = None
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            raise error("invalid Content-Length")
        digits = length.lstrip("0") or "0"
        if len(digits) > len(str(limit)) or (expected := int(digits)) > limit:
            raise error("response exceeds decoded size limit")
    size = 0
    data = BytesIO()
    if response.is_stream_consumed:
        # Some transports deliver already-decoded content.
        content = response.content
        size = len(content)
    elif encoding == "identity":
        async for chunk in response.aiter_raw():
            size += len(chunk)
            if size > limit:
                raise error("response exceeds decoded size limit")
            data.write(chunk)
        content = data.getvalue()
    else:
        # HTTPX's decoder expands an entire input chunk before yielding.
        decoder = zlib.decompressobj(wbits=31)
        try:
            async for raw in response.aiter_raw():
                while raw:
                    if decoder.eof:
                        decoder = zlib.decompressobj(wbits=31)
                    chunk = decoder.decompress(raw, limit - size + 1)
                    size += len(chunk)
                    if size > limit:
                        raise error("response exceeds decoded size limit")
                    data.write(chunk)
                    raw = decoder.unused_data
        except zlib.error as exc:
            raise error("invalid gzip response", type(exc).__name__) from None
        if not decoder.eof:
            raise error("incomplete gzip response")
        content = data.getvalue()
    if size > limit:
        raise error("response exceeds decoded size limit")
    if expected is not None and expected != size:
        raise error("Content-Length mismatch")
    return content


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
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, identity"},
            timeout=30,
            transport=transport,
            event_hooks={"request": [self._limit]},
        )

    async def _limit(self, request: httpx.Request) -> None:
        if limiter := self._limiters.get(request.url.host):
            async with limiter:
                pass

    async def get_json(
        self, url: str, params: Mapping[str, str] | None = None, *, limit: int = _JSON_LIMIT
    ) -> Any:
        response, content, service = await self._request(
            "GET", url, params=params, limit=limit, follow_redirects=True
        )
        return self._json(response, content, service)

    async def post_form(
        self, url: str, data: Mapping[str, str], *, follow_redirects: bool = False
    ) -> Any:
        response, content, service = await self._request(
            "POST", url, data=data, follow_redirects=follow_redirects
        )
        return self._json(response, content, service)

    async def api_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: object = None,
        params: Mapping[str, str] | None = None,
        service: str | None = None,
    ) -> object:
        response, content, name = await self._request(
            method, url, params=params, json=body, request_headers=headers, service=service
        )
        return self._json(response, content, name) if content else None

    async def get_bytes(self, url: str) -> bytes:
        _, content, _ = await self._request(
            "GET",
            url,
            request_headers={"Accept-Encoding": "identity"},
            limit=_COVER_LIMIT,
            follow_redirects=True,
        )
        return content

    @staticmethod
    def _json(response: httpx.Response, content: bytes, service: str) -> Any:
        try:
            return json.loads(content)
        except (ValueError, RecursionError) as exc:
            raise HttpError(
                response.url,
                response.status_code,
                "invalid JSON response",
                service=service,
                kind=type(exc).__name__,
            ) from None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        limit: int = _JSON_LIMIT,
        json: object = None,
        request_headers: Mapping[str, str] | None = None,
        follow_redirects: bool = False,
        service: str | None = None,
    ) -> tuple[httpx.Response, bytes, str]:
        host = httpx.URL(url).host
        name = service or _SERVICES.get(host, host)
        last = HttpError(url, None, "no HTTP attempts configured", service=name)
        for attempt in range(self._max_attempts):
            delay = None
            request = self._client.build_request(
                method, url, params=params, data=data, headers=request_headers, json=json
            )
            code = None
            try:
                for hops in range(self._client.max_redirects + 1):
                    code = None
                    response = await self._client.send(request, stream=True, follow_redirects=False)
                    code = response.status_code
                    try:
                        if follow_redirects and response.next_request is not None:
                            target = response.next_request
                            if target.url.scheme != "https":
                                raise HttpError(
                                    target.url, code, "redirect requires HTTPS", service=name
                                )
                            if hops == self._client.max_redirects:
                                raise HttpError(
                                    target.url, code, "too many redirects", service=name
                                )
                            request = target
                            continue
                        if response.is_success:
                            return response, await _read(response, limit, name), name
                        last = HttpError(
                            request.url, code, "unexpected HTTP response", service=name
                        )
                        if code != 429 and not response.is_server_error:
                            raise last
                        if code in {429, 503} and (header := response.headers.get("Retry-After")):
                            delay = _retry_after(header)
                        break
                    finally:
                        await response.aclose()
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                last = HttpError(
                    request.url, code, "request failed", service=name, kind=type(exc).__name__
                )
            if attempt + 1 == self._max_attempts:
                raise last from None
            if delay is None:
                delay = min(60, 2**attempt) * (0.5 + self._rand())
            await self._sleep(delay)
        raise last

    async def aclose(self) -> None:
        await self._client.aclose()
