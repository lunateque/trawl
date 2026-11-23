from collections import Counter, deque
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Download:
    at: datetime
    source: str
    title: str
    started: bool


class Progress:
    def __init__(self) -> None:
        self.active: Counter[str] = Counter()
        self.events: deque[Download] = deque(maxlen=200)

    @contextmanager
    def downloading(self, source: str, title: str) -> Generator[None]:
        self.active[source] += 1
        self.events.append(Download(datetime.now(UTC), source, title, True))
        try:
            yield
        finally:
            self.active[source] -= 1
            self.events.append(Download(datetime.now(UTC), source, title, False))
