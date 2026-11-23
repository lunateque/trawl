import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import pytest


def mbid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"trawl-tests:{label}"))


@pytest.fixture(autouse=True)
def require_audio_tools(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("tools") is None:
        return
    missing = [name for name in ("ffmpeg", "ffprobe", "fpcalc", "rsgain") if not shutil.which(name)]
    if missing:
        message = f"Missing audio tools: {', '.join(missing)}"
        if os.environ.get("CI", "").casefold() == "true":
            pytest.fail(message)
        pytest.skip(message)


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "sine.flac"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(  # noqa: S603
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=d=31", str(path)], check=True
    )
    return path


@pytest.fixture
def jpeg(tmp_path: Path) -> bytes:
    path = tmp_path / "test.jpg"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=16x16",
            "-frames:v",
            "1",
            "-threads",
            "1",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


@pytest.fixture
def mb_fixture() -> Callable[[str], dict[str, Any]]:
    def read(name: str) -> dict[str, Any]:
        path = Path(__file__).parent / "fixtures" / "mb" / f"{name}.json"
        return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))

    return read
