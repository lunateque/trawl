import asyncio
from collections import deque
from email.utils import formatdate
from time import time

import httpx
import pytest

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
