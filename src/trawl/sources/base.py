from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from trawl.mb.models import Release, Track


@dataclass(frozen=True)
class CandidateItem:
    ref: str
    position: int | None
    length_ms: int | None
    title: str
    disc: int | None = None
    aliases: tuple[str, ...] = ()
    size: int | None = None
    expected_length_ms: int | None = None


@dataclass(frozen=True)
class Candidate:
    source: str
    ref: str
    kind: Literal["album", "track"]
    title: str
    codec: str | None
    bitrate: int | None
    lossless: bool
    official: bool
    items: tuple[CandidateItem, ...]
    score: float = 0
    free_slot: bool = False
    upload_speed: int = 0


@dataclass(frozen=True)
class Fetched:
    item_ref: str
    path: Path
    codec: str
    bitrate: int | None
    lossless: bool


class Source(Protocol):
    name: str

    async def search(self, release: Release, tracks: list[Track]) -> list[Candidate]: ...
    async def fetch(self, candidate: Candidate, into: Path) -> list[Fetched]: ...


class SourceWall(Exception):  # noqa: N818 - public source protocol
    @property
    def reason(self) -> str:
        return str(self)


class FetchError(Exception):
    pass


@dataclass(frozen=True)
class FetchIssue:
    reason: str
    probe_failed: bool = False


class PartialFetchError(FetchError):
    def __init__(
        self,
        files: list[Fetched],
        issues: dict[str, FetchIssue],
        wall: SourceWall | None,
    ) -> None:
        super().__init__("some files could not be fetched")
        self.files = files
        self.issues = issues
        self.wall = wall
