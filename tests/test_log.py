import json
import logging
from pathlib import Path

from trawl.log import setup


def test_log_records_fields_and_level(tmp_path: Path) -> None:
    logger = logging.getLogger("trawl.plan")
    with setup(tmp_path, verbose=False):
        logger.debug("hidden_on_console", extra={"fields": {"want_id": 3}})
    entry = json.loads((tmp_path / "trawl.log").read_text(encoding="utf-8"))
    assert entry["event"] == "hidden_on_console"
    assert entry["level"] == "DEBUG"
    assert entry["want_id"] == 3
    assert entry["ts"].endswith("+00:00")
