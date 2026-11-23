import json
from pathlib import Path

import httpx
import pytest
from test_folder_sources import album, tone

from trawl.net.client import HttpClient
from trawl.sources.base import FetchIssue, PartialFetchError
from trawl.sources.soulseek import SoulseekSource


@pytest.mark.tools
@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "stall", "partial_stall"])
async def test_soulseek_search_and_transfer(tmp_path: Path, outcome: str) -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures" / "slskd.json").read_text())
    download = tmp_path / "download"
    first = download / "CD1" / "01 First Song.flac"
    second = download / "CD 2" / "01 Second Song.flac"
    if outcome in {"success", "partial_stall"}:
        tone(first, "flac")
        fixture["search"][0]["files"][0]["size"] = first.stat().st_size
    if outcome == "success":
        tone(second, "flac")
        fixture["search"][0]["files"][1]["size"] = second.stat().st_size
    requested: list[dict[str, object]] = []
    deleted: list[str] = []
    observed: list[httpx.Request] = []
    ticks = 0.0

    async def sleep(seconds: float) -> None:
        nonlocal ticks
        ticks += seconds

    def handle(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        assert request.headers["X-API-Key"] == "fixture"
        path = request.url.path
        if request.method == "DELETE":
            deleted.append(path)
            return httpx.Response(204)
        if path == "/api/v0/searches":
            assert json.loads(request.content)["searchText"] == "The Band The Album"
            return httpx.Response(201)
        if path.endswith("/responses"):
            return httpx.Response(200, json=fixture["search"])
        if "/searches/" in path:
            return httpx.Response(200, json={"isComplete": True})
        assert path.endswith("/downloads/peer one")
        if request.method == "POST":
            requested.extend(json.loads(request.content))
            return httpx.Response(201)
        return httpx.Response(
            200,
            json={
                "directories": [
                    {
                        "directory": "Shared",
                        "files": [
                            {
                                "id": str(n),
                                "filename": row["filename"],
                                "state": fixture["transfers"][
                                    ("stall" if n else "success")
                                    if outcome == "partial_stall"
                                    else outcome
                                ],
                                "bytesTransferred": 0,
                            }
                            for n, row in enumerate(requested)
                        ],
                    }
                ]
            },
        )

    http = HttpClient("tests", {}, transport=httpx.MockTransport(handle))
    try:
        source = SoulseekSource(
            http,
            "http://test",
            "fixture",
            download,
            stall_timeout_s=2,
            clock=lambda: ticks,
            sleep=sleep,
        )
        release = album()
        candidates = await source.search(
            release, [track for disc in release.media for track in disc.tracks]
        )
        assert len(candidates) == 1 and candidates[0].lossless and candidates[0].free_slot
        stage = tmp_path / "stage"
        stage.mkdir()
        if outcome == "success":
            files = await source.fetch(candidates[0], stage)
            assert len(files) == 2 and all(file.path.is_file() for file in files)
            assert not first.exists() and not second.exists()
        else:
            with pytest.raises(PartialFetchError) as failure:
                await source.fetch(candidates[0], stage)
            assert not failure.value.files
            if outcome in {"stall", "partial_stall"}:
                assert len(failure.value.issues) == 2
                assert set(failure.value.issues.values()) == {FetchIssue("stalled")}
                assert sum("/downloads/" in path for path in deleted) == 2
                assert ticks == 2
        assert len(requested) == 2 and all(
            str(item["filename"]).endswith(".flac") for item in requested
        )
        assert any("/searches/" in path for path in deleted)
    finally:
        await http.aclose()
