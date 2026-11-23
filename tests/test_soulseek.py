import json
from pathlib import Path

import httpx
import pytest
from test_folder_sources import album, tone

from trawl.core.fetch import FetchIssue, PartialFetchError
from trawl.net.client import HttpClient
from trawl.sources.soulseek import SoulseekSource

_WRONG_SIZE = b"wrong size"


def _prepare_audio(tmp_path: Path, first: Path, second: Path, outcome: str) -> dict[Path, bytes]:
    files: dict[Path, bytes] = {}
    for n, dest in enumerate((first, second)):
        if outcome == "success" or (outcome.startswith("partial_stall") and dest == first):
            original = tmp_path / f"{n}.flac"
            tone(original, "flac")
            files[dest] = original.read_bytes()
    if outcome == "partial_stall_extra":
        first.parent.mkdir(parents=True)
        (first.parent / "not-requested.flac").write_bytes(b"unrelated audio")
    if outcome == "partial_stall_wrong_size":
        files[first] += _WRONG_SIZE
    return files


def _assert_cleanup(first: Path, outcome: str) -> None:
    if outcome not in {"partial_stall", "partial_stall_extra"}:
        assert first.is_file()
        return
    assert not first.exists()
    if outcome == "partial_stall_extra":
        assert (first.parent / "not-requested.flac").read_bytes() == b"unrelated audio"
    else:
        assert not first.parent.exists()


def _search_response(request: httpx.Request, results: object) -> httpx.Response | None:
    path = request.url.path
    if path == "/api/v0/searches":
        assert json.loads(request.content)["searchText"] == "The Band The Album"
        return httpx.Response(201)
    if path.endswith("/responses"):
        return httpx.Response(200, json=results)
    if "/searches/" in path:
        return httpx.Response(200, json={"isComplete": True})
    return None


def _assert_failed(
    failure: PartialFetchError, outcome: str, deleted: list[str], ticks: float, first: Path
) -> None:
    assert not failure.files
    partial = outcome.startswith("partial_stall")
    if outcome == "stall" or partial:
        assert len(failure.issues) == 2
        assert set(failure.issues.values()) == {FetchIssue("stalled")}
        assert sorted(path.rsplit("/", 1)[1] for path in deleted if "/downloads/" in path) == (
            ["1"] if outcome == "partial_stall_old_id" else ["0", "1"]
        )
        assert ticks == 2
    if partial:
        _assert_cleanup(first, outcome)


@pytest.mark.tools
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "error",
        "stall",
        "partial_stall",
        "partial_stall_extra",
        "partial_stall_wrong_size",
        "partial_stall_preexisting",
        "partial_stall_old_id",
    ],
)
async def test_soulseek_search_and_transfer(tmp_path: Path, outcome: str) -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures" / "slskd.json").read_text())
    download = tmp_path / "download"
    first = download / "CD1" / "01 First Song.flac"
    second = download / "CD 2" / "01 Second Song.flac"
    partial = outcome.startswith("partial_stall")
    staged_by_slskd = _prepare_audio(tmp_path, first, second, outcome)
    for n, dest in enumerate((first, second)):
        if dest in staged_by_slskd:
            fixture["search"][0]["files"][n]["size"] = len(staged_by_slskd[dest]) - (
                len(_WRONG_SIZE) if outcome == "partial_stall_wrong_size" else 0
            )
    if outcome == "partial_stall_preexisting":
        first.parent.mkdir(parents=True)
        first.write_bytes(staged_by_slskd.pop(first))
    old_transfer = {
        "id": "old",
        "filename": fixture["search"][0]["files"][0]["filename"],
        "state": "Completed, Succeeded",
        "bytesTransferred": 0,
    }
    prior = [old_transfer] if outcome == "partial_stall_old_id" else []
    requested: list[dict[str, object]] = []
    deleted: list[str] = []
    ticks = 0.0

    async def sleep(seconds: float) -> None:
        nonlocal ticks
        ticks += seconds

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "fixture"
        path = request.url.path
        if request.method == "DELETE":
            deleted.append(path)
            return httpx.Response(204)
        if response := _search_response(request, fixture["search"]):
            return response
        assert path.endswith("/downloads/peer one")
        if request.method == "POST":
            requested.extend(json.loads(request.content))
            for dest, audio in staged_by_slskd.items():
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(audio)
            return httpx.Response(201)
        if not requested:
            return httpx.Response(200 if prior else 404, json=prior)
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
                                    ("stall" if n else "success") if partial else outcome
                                ],
                                "bytesTransferred": 0,
                            }
                            for n, row in enumerate(requested)
                            if n or outcome != "partial_stall_old_id"
                        ]
                        + prior,
                    }
                ]
            },
        )

    http = HttpClient("tests", {}, transport=httpx.MockTransport(handle))
    try:
        source = SoulseekSource(
            http,
            "https://test",
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
            _assert_failed(failure.value, outcome, deleted, ticks, first)
        assert len(requested) == 2 and all(
            str(item["filename"]).endswith(".flac") for item in requested
        )
        assert any("/searches/" in path for path in deleted)
    finally:
        await http.aclose()
