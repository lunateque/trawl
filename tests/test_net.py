import asyncio
from collections import deque
from email.utils import formatdate
from time import time

import httpx
import pytest

from trawl.config.models import ProxyConfig
from trawl.net.client import HttpClient, HttpError
from trawl.net.limiter import RateLimiter

URL = "https://musicbrainz.org/ws/2/artist"


@pytest.mark.parametrize(
    ("responses", "delays", "status"),
    [
        ([200], [], None),
        ([(503, "12"), 200], [12], None),
        ([(429, formatdate(time() + 3600, usegmt=True)), 200], [300], None),
        ([(429, formatdate(0, usegmt=True)), 200], [0], None),
        ([500] * 5, [1, 2, 4, 8], 500),
        ([404], [], 404),
        ([None, 200], [1], None),
        ([429, 200], [1], None),
    ],
)
async def test_http_retries(
    responses: list[int | tuple[int, str] | None],
    delays: list[float],
    status: int | None,
) -> None:
    queue = deque(responses)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"] == "trawl/test"
        assert request.url.params["fmt"] == "json"
        response = queue.popleft()
        if response is None:
            raise httpx.ConnectError("not reachable", request=request)
        if isinstance(response, tuple):
            code, retry_after = response
            return httpx.Response(code, headers={"Retry-After": retry_after})
        return httpx.Response(response, json={"ok": True})

    http = HttpClient("trawl/test", {}, httpx.MockTransport(handler), sleep, lambda: 0.5)
    try:
        if status is None:
            assert await http.get_json(URL, {"fmt": "json"}) == {"ok": True}
        else:
            with pytest.raises(HttpError) as error:
                await http.get_json(URL, {"fmt": "json"})
            assert error.value.status == status
            assert error.value.url.startswith(URL)
        assert slept == delays
        assert not queue
    finally:
        await http.aclose()


async def test_limiter_spaces_concurrent_calls_and_redirects() -> None:
    now = 0.0
    starts: list[tuple[str, float]] = []

    async def sleep(seconds: float) -> None:
        nonlocal now
        await asyncio.sleep(0)
        now += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append((request.url.host, now))
        if request.url.path == "/redirect":
            return httpx.Response(307, headers={"Location": URL})
        return httpx.Response(200, json={})

    http = HttpClient(
        "test",
        {"musicbrainz.org": RateLimiter(1, lambda: now, sleep)},
        httpx.MockTransport(handler),
    )
    try:
        await asyncio.gather(http.get_json(URL), http.get_json("https://other.org/redirect"))
    finally:
        await http.aclose()
    assert starts == [("musicbrainz.org", 0), ("other.org", 0), ("musicbrainz.org", 1)]


@pytest.mark.parametrize("enabled", [True, False])
async def test_proxy_routes_each_redirect_hop(enabled: bool) -> None:
    seen: list[tuple[str, str]] = []
    hops = {
        "coverartarchive.org": "https://archive.org/redirect",
        "archive.org": "https://ia800100.archive.org/redirect",
        "ia800100.archive.org": "https://notarchive.org/redirect",
        "notarchive.org": "https://archive.org.evil.test/redirect",
        "archive.org.evil.test": "https://other.test/result",
    }

    def transport(route: str) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            seen.append((route, request.url.host))
            if request.url.path == "/insecure":
                return httpx.Response(307, headers={"Location": "http://archive.org/result"})
            if target := hops.get(request.url.host):
                return httpx.Response(307, headers={"Location": target})
            return httpx.Response(200, content=b"image")

        return httpx.MockTransport(handle)

    proxy = ProxyConfig(
        "http://proxy.test:8888" if enabled else "",
        ("coverartarchive.org", "archive.org", "*.archive.org"),
    )
    http = HttpClient(
        "test", {}, transport("direct"), proxy=proxy, proxy_transport=transport("proxy")
    )
    try:
        assert await http.get_bytes("https://coverartarchive.org/redirect") == b"image"
        with pytest.raises(HttpError, match="redirect requires HTTPS"):
            await http.get_bytes("https://coverartarchive.org/insecure")
    finally:
        await http.aclose()
    routed = "proxy" if enabled else "direct"
    assert seen == [
        (routed, "coverartarchive.org"),
        (routed, "archive.org"),
        (routed, "ia800100.archive.org"),
        ("direct", "notarchive.org"),
        ("direct", "archive.org.evil.test"),
        ("direct", "other.test"),
        (routed, "coverartarchive.org"),
    ]
