import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest


@pytest.fixture
def mb_fixture() -> Callable[[str], dict[str, Any]]:
    def read(name: str) -> dict[str, Any]:
        path = Path(__file__).parent / "fixtures" / "mb" / f"{name}.json"
        return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))

    return read
