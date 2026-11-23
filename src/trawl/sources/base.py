from pathlib import Path
from typing import Protocol

from trawl.core.catalog import Release, Track
from trawl.core.fetch import Candidate, Fetched


class Source(Protocol):
    name: str

    async def search(self, release: Release, tracks: list[Track]) -> list[Candidate]: ...
    async def fetch(self, candidate: Candidate, into: Path) -> list[Fetched]: ...
