import json
import logging
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

from rich.console import Console
from rich.logging import RichHandler


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields: object = getattr(record, "fields", None)
        entry = dict(cast("Mapping[str, object]", fields)) if isinstance(fields, Mapping) else {}
        entry.update(
            ts=datetime.fromtimestamp(record.created, UTC).isoformat(),
            level=record.levelname,
            event=record.getMessage(),
        )
        return json.dumps(entry, ensure_ascii=False)


@contextmanager
def setup(data_dir: Path, verbose: bool, *, terminal: bool = True) -> Generator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("trawl")
    previous_level, previous_propagate = logger.level, logger.propagate
    file = RotatingFileHandler(
        data_dir / "trawl.log", maxBytes=10 * 1024**2, backupCount=3, encoding="utf-8"
    )
    file.setFormatter(_JsonFormatter())
    console = RichHandler(console=Console(stderr=True), show_path=False) if terminal else None
    if console is not None:
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        logger.addHandler(console)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(file)
    try:
        yield
    finally:
        if console is not None:
            logger.removeHandler(console)
            console.close()
        logger.removeHandler(file)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        file.close()
