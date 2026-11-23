import os
from collections import deque
from datetime import UTC, datetime

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static, TabbedContent, TabPane

from trawl.config.models import Config
from trawl.db import queue
from trawl.db.states import TrackState
from trawl.runner.progress import Progress
from trawl.tui.data import Event, Snapshot
from trawl.tui.dialogs import Grid


def status_line(owner: str | None, running: bool, stopping: bool) -> str:
    if owner is not None:
        return f"observer (pid {owner})"
    if running:
        return "stopping" if stopping else f"running (pid {os.getpid()})"
    return "stopped"


def source_lines(cfg: Config, owner: str | None, progress: Progress, view: Snapshot) -> str:
    lines = ["Sources · last hour"]
    for row in view.sources:
        if row.pause:
            delay = max(
                0,
                int((datetime.fromisoformat(row.pause.until) - datetime.now(UTC)).total_seconds()),
            )
            activity = f"paused {delay // 60}m {delay % 60}s · {row.pause.reason}"
        elif owner is not None:
            activity = "observing"
        elif active := progress.active[row.name]:
            limit = {
                "ytmusic": cfg.sources.ytmusic_concurrency,
                "soulseek": cfg.sources.soulseek.concurrency,
            }.get(row.name)
            activity = f"{active}/{limit} downloading" if limit else f"{active} downloading"
        else:
            activity = "idle"
        lines.append(f"{row.name:<9} ok {row.accepted:>3}  rej {row.rejected:>3}  {activity}")
    return "\n".join(lines)


def pin_label(raw: str | None) -> str:
    if raw is None:
        return "No staged file"
    pin = queue.Pin.load(raw)
    return f"Pinned · {pin.source}: {pin.item_ref or pin.ref}"


def queue_counts(view: Snapshot) -> str:
    labels = (*TrackState, "unresolved")
    counts = [f"{name:<11} {view.counts.get(name, 0):>5}" for name in labels[:-1]]
    counts.append(f"unresolved  {len(view.unresolved):>5}")
    return "Queue\n" + "\n".join(counts)


def queue_rows(view: Snapshot) -> tuple[list[tuple[str, ...]], ...]:
    missing: list[tuple[str, ...]] = [
        (str(track.id), track.title, track.release, track.attempts or "No attempts")
        for track in view.tracks.values()
        if track.state is TrackState.MISSING
    ]
    rejected: list[tuple[str, ...]] = [
        (
            str(track.id),
            track.title,
            track.release,
            rejection.reason if rejection else pin_label(track.pin),
        )
        for track_id, rejection in sorted(
            view.rejected.items(), key=lambda item: (item[1] is None, item[0])
        )
        if (track := view.tracks.get(track_id))
    ]
    unresolved: list[tuple[str, ...]] = [
        (
            str(row.id),
            row.origin,
            row.artist or "—",
            row.title or row.album or row.ref,
            row.reason,
        )
        for row in view.unresolved.values()
    ]
    return missing, rejected, unresolved


def collect_events(progress: Progress, live: deque[Event], sequence: int) -> int:
    while progress.events:
        transfer = progress.events.popleft()
        sequence += 1
        live.append(
            Event(
                f"live-{sequence}",
                transfer.at,
                "downloading" if transfer.started else "download ended",
                f"{transfer.source} · {transfer.title}",
                None,
            )
        )
    return sequence


def visible_events(view: Snapshot, live: deque[Event]) -> list[Event]:
    return sorted(
        (*view.events, *reversed(live)),
        key=lambda event: event.at,
        reverse=True,
    )[:200]


def event_rows(events: list[Event]) -> list[tuple[str, ...]]:
    return [
        (event.id, event.at.astimezone().strftime("%H:%M"), event.label, event.detail)
        for event in events
    ]


def compose_dashboard() -> ComposeResult:
    yield Static(id="status", markup=False)
    with Horizontal(id="overview"):
        yield Static(id="counts", markup=False)
        with Vertical(id="activity"):
            yield Static(id="sources", markup=False)
            yield Static("Events · newest first", classes="heading")
            yield Grid("events")
    yield Static("Needs you · 1 / 2 / 3 to switch · Tab to focus Events", id="needs")
    with TabbedContent(initial="tab-missing"):
        with TabPane("Missing", id="tab-missing"):
            yield Grid("missing")
        with TabPane("Rejected", id="tab-rejected"):
            yield Grid("rejected")
        with TabPane("Unresolved", id="tab-unresolved"):
            yield Grid("unresolved")
    yield Static(
        "r retry · s skip · p pick · a accept · l link · e explain · w watch · q quit",
        id="keys",
    )
