import asyncio
import os
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import ExitStack, suppress
from datetime import UTC, datetime
from typing import ClassVar, override

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Static, TabbedContent, TabPane
from textual.worker import Worker, WorkerCancelled, WorkerFailed, WorkerState

from trawl.config import Config
from trawl.db import queue, repo, review
from trawl.db.states import Actor, TrackState, transition
from trawl.plan.wants import MbLink, parse_add_target
from trawl.runner.lock import LockBusy, acquire
from trawl.runner.progress import Progress
from trawl.taste.store import bind
from trawl.tui.data import (
    Event,
    Snapshot,
    TrackRow,
    UnresolvedRow,
    explain_text,
    revision,
    snapshot,
)
from trawl.tui.dialogs import Candidates, Details, Grid, Paste

type Watch = Callable[[asyncio.Event, Progress], Awaitable[None]]


class TrawlApp(App[None]):
    TITLE = "trawl"
    CSS_PATH = "dashboard.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "retry", "Retry missing"),
        Binding("s", "skip", "Skip"),
        Binding("p", "pick", "Pick candidate"),
        Binding("a", "accept", "Accept file"),
        Binding("l", "link", "Link unresolved"),
        Binding("e", "explain", "Explain"),
        Binding("w", "toggle_watch", "Toggle watch"),
        Binding("1", "tab('missing')", "Missing"),
        Binding("2", "tab('rejected')", "Rejected"),
        Binding("3", "tab('unresolved')", "Unresolved"),
        Binding("q", "leave", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, cfg: Config, conn: sqlite3.Connection, watch: Watch | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.conn = conn
        self._watch_job = watch
        self.snapshot = Snapshot({}, [], [], {}, {}, {})
        self._lock = ExitStack()
        self._owner: str | None = None
        self._main: Screen[object] | None = None
        self._stop = asyncio.Event()
        self._worker: Worker[None] | None = None
        self._progress = Progress()
        self._live: deque[Event] = deque(maxlen=200)
        self._sequence = 0
        self._visible_events: list[Event] = []
        self._rows: dict[str, list[tuple[str, ...]]] = {}

    def compose(self) -> ComposeResult:
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

    def on_mount(self) -> None:
        self._main = self.screen
        for name, columns in (
            ("events", ("Time", "Event", "Track / detail")),
            ("missing", ("#", "Track", "Release", "Recent attempts")),
            ("rejected", ("#", "Track", "Release", "Reason / pinned candidate")),
            ("unresolved", ("#", "Origin", "Artist", "Title / album", "Reason")),
        ):
            self._main.query_one(f"#{name}", Grid).add_columns(*columns)
        try:
            self._lock.enter_context(acquire(self.cfg.data_dir))
        except LockBusy as exc:
            self._owner = exc.pid
        if self._owner is None and self._watch_job is not None:
            self._start_watch()
        self.refresh_dashboard()
        self._main.query_one("#missing", Grid).focus()
        self.set_interval(2, self.refresh_dashboard)

    def on_unmount(self) -> None:
        self._lock.close()

    def _start_watch(self) -> None:
        if self._watch_job is not None:
            self._stop = asyncio.Event()
            self._worker = self.run_worker(
                self._watch_job(self._stop, self._progress), exit_on_error=False
            )

    @on(Worker.StateChanged)
    def watch_state(self, event: Worker.StateChanged) -> None:
        if (
            event.state is WorkerState.ERROR
            and self._worker is not None
            and self._worker.is_finished
        ):
            self.notify("Watch stopped with an error; see trawl.log", severity="error")
            self.refresh_dashboard()

    def _watch_running(self) -> bool:
        return self._worker is not None and not self._worker.is_finished

    async def _stop_watch(self) -> None:
        if self._worker is None:
            return
        self._stop.set()
        self.refresh_dashboard()
        with suppress(WorkerCancelled, WorkerFailed):
            await self._worker.wait()
        self._worker = None

    async def action_toggle_watch(self) -> None:
        if self._owner is not None:
            self.notify("Watch is controlled by the other runner")
            return
        if self._watch_running():
            await self._stop_watch()
        else:
            self._start_watch()
        self.refresh_dashboard()

    @override
    async def action_quit(self) -> None:
        await self._stop_watch()
        self._lock.close()
        self.exit()

    async def action_leave(self) -> None:
        await self.action_quit()

    @override
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {
            "tab",
            "leave",
            "retry",
            "skip",
            "pick",
            "accept",
            "link",
            "explain",
            "toggle_watch",
        }:
            return self.screen is self._main
        return super().check_action(action, parameters)

    @on(TabbedContent.TabActivated)
    def focus_tab(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id is not None and self.screen is self._main:
            event.pane.query_one(Grid).focus()

    def action_tab(self, name: str) -> None:
        if self._main is not None:
            self._main.query_one(TabbedContent).active = f"tab-{name}"
            self._main.query_one(f"#{name}", Grid).focus()

    def _replace(self, grid: Grid, rows: list[tuple[str, ...]]) -> None:
        name = grid.id or ""
        if self._rows.get(name) == rows:
            return
        self._rows[name] = rows
        cursor = grid.cursor_row
        key = (
            grid.coordinate_to_cell_key(grid.cursor_coordinate).row_key.value
            if grid.row_count
            else None
        )
        grid.clear()
        for row in rows:
            grid.append(row[0], *(row[1:] if grid.id == "events" else row))
        if rows:
            selected = next((i for i, row in enumerate(rows) if row[0] == key), None)
            grid.move_cursor(row=min(cursor, len(rows) - 1) if selected is None else selected)

    def _status_line(self) -> str:
        if self._owner is not None:
            return f"observer (pid {self._owner})"
        if self._watch_running():
            return "stopping" if self._stop.is_set() else f"running (pid {os.getpid()})"
        return "stopped"

    def _source_lines(self, view: Snapshot) -> str:
        lines = ["Sources · last hour"]
        for row in view.sources:
            if row.pause:
                delay = max(
                    0,
                    int(
                        (
                            datetime.fromisoformat(row.pause.until) - datetime.now(UTC)
                        ).total_seconds()
                    ),
                )
                activity = f"paused {delay // 60}m {delay % 60}s · {row.pause.reason}"
            elif self._owner is not None:
                activity = "observing"
            elif active := self._progress.active[row.name]:
                limit = {
                    "ytmusic": self.cfg.sources.ytmusic_concurrency,
                    "soulseek": self.cfg.sources.soulseek.concurrency,
                }.get(row.name)
                activity = f"{active}/{limit} downloading" if limit else f"{active} downloading"
            else:
                activity = "idle"
            lines.append(f"{row.name:<9} ok {row.accepted:>3}  rej {row.rejected:>3}  {activity}")
        return "\n".join(lines)

    def _event_rows(self, view: Snapshot) -> list[tuple[str, ...]]:
        while self._progress.events:
            transfer = self._progress.events.popleft()
            self._sequence += 1
            self._live.append(
                Event(
                    f"live-{self._sequence}",
                    transfer.at,
                    "downloading" if transfer.started else "download ended",
                    f"{transfer.source} · {transfer.title}",
                    None,
                )
            )
        self._visible_events = sorted(
            (*view.events, *reversed(self._live)),
            key=lambda event: event.at,
            reverse=True,
        )[:200]
        return [
            (event.id, event.at.astimezone().strftime("%H:%M"), event.label, event.detail)
            for event in self._visible_events
        ]

    def refresh_dashboard(self) -> None:
        main = self._main
        if main is None:
            return
        with self.conn:
            self.conn.execute("BEGIN")
            view = self.snapshot = snapshot(self.conn, self.cfg)
        main.query_one("#status", Static).update(
            f" trawl ─ runner: {self._status_line()} │ library {self.cfg.library_root} "
            f"│ {datetime.now().strftime('%H:%M')}"
        )
        labels = (*TrackState, "unresolved")
        counts = [f"{name:<11} {view.counts.get(name, 0):>5}" for name in labels[:-1]]
        counts.append(f"unresolved  {len(view.unresolved):>5}")
        main.query_one("#counts", Static).update("Queue\n" + "\n".join(counts))
        main.query_one("#sources", Static).update(self._source_lines(view))
        missing = [
            (str(track.id), track.title, track.release, track.attempts or "No attempts")
            for track in view.tracks.values()
            if track.state is TrackState.MISSING
        ]
        rejected = [
            (
                str(track.id),
                track.title,
                track.release,
                rejection.reason if rejection else self._pin_label(track.pin),
            )
            for track_id, rejection in sorted(
                view.rejected.items(), key=lambda item: (item[1] is None, item[0])
            )
            if (track := view.tracks.get(track_id))
        ]
        unresolved = [
            (
                str(row.id),
                row.origin,
                row.artist or "—",
                row.title or row.album or row.ref,
                row.reason,
            )
            for row in view.unresolved.values()
        ]
        tabs = main.query_one(TabbedContent)
        for label, rows in (
            ("Missing", missing),
            ("Rejected", rejected),
            ("Unresolved", unresolved),
        ):
            name = label.lower()
            tabs.get_tab(f"tab-{name}").label = f"{label} {len(rows)}"
            self._replace(main.query_one(f"#{name}", Grid), rows)
        event_table = main.query_one("#events", Grid)
        self._replace(event_table, self._event_rows(view))

    @staticmethod
    def _pin_label(raw: str | None) -> str:
        if raw is None:
            return "No staged file"
        pin = queue.Pin.load(raw)
        return f"Pinned · {pin.source}: {pin.item_ref or pin.ref}"

    def _selection(self) -> tuple[str, int] | None:
        if self._main is None:
            return None
        focused = self.focused
        if isinstance(focused, Grid) and focused.id == "events":
            if focused.row_count:
                event = self._visible_events[focused.cursor_row]
                if event.track_id is not None:
                    return "events", event.track_id
            return None
        name = self._main.query_one(TabbedContent).active.removeprefix("tab-")
        grid = self._main.query_one(f"#{name}", Grid)
        if grid.row_count:
            key = grid.coordinate_to_cell_key(grid.cursor_coordinate).row_key.value
            if key is not None:
                return name, int(key)
        return None

    def _track(self, selection: tuple[str, int] | None) -> TrackRow | None:
        if selection is not None and selection[0] != "unresolved":
            return self.snapshot.tracks.get(selection[1])
        return None

    def _unresolved(self) -> UnresolvedRow | None:
        selected = self._selection()
        if selected is not None and selected[0] == "unresolved":
            return self.snapshot.unresolved[selected[1]]
        return None

    def _write(self, shown: TrackRow, change: Callable[[], bool]) -> None:
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            changed = revision(self.conn, shown.id) == shown.revision and change()
        if not changed:
            self.notify("already changed by the runner", severity="warning")
        self.refresh_dashboard()

    def action_retry(self) -> None:
        selected = self._selection()
        if selected is not None and selected[0] == "missing" and (row := self._track(selected)):
            self._write(
                row,
                lambda: transition(
                    self.conn,
                    row.id,
                    TrackState.MISSING,
                    TrackState.PLANNED,
                    Actor.USER,
                    expected_pin=row.pin,
                    path=None,
                    pinned_candidate=None,
                    next_try_at=None,
                ),
            )

    def action_skip(self) -> None:
        if (row := self._track(self._selection())) and row.state is not TrackState.DONE:
            self._write(row, lambda: queue.skip(self.conn, row.id) == 1)

    def action_explain(self) -> None:
        if row := self._track(self._selection()):
            self.push_screen(
                Details(row.title, explain_text(repo.explain(self.conn, None, row.id)))
            )
        elif unresolved := self._unresolved():
            self.push_screen(
                Details(
                    unresolved.title or unresolved.ref,
                    f"{unresolved.origin}: {unresolved.ref}\n{unresolved.reason}",
                )
            )

    def action_pick(self) -> None:
        selected = self._selection()
        if selected is None or selected[0] not in {"missing", "rejected"}:
            return
        if not (row := self._track(selected)):
            return

        def pin(value: review.Choice | str | None) -> None:
            if value is None:
                return
            try:
                candidate = (
                    review.parse_pin(value, self.cfg.sources.order)
                    if isinstance(value, str)
                    else queue.Pin(value.source, value.ref, value.item_ref, value.size)
                )
            except (OSError, ValueError) as exc:
                self.notify(str(exc), severity="warning")
                return
            self._write(
                row,
                lambda: queue.pin(
                    self.conn,
                    row.id,
                    candidate.source,
                    candidate.ref,
                    item_ref=candidate.item_ref,
                    size=candidate.size,
                ),
            )

        self.push_screen(Candidates(row.title, review.choices(self.conn, row.id)), pin)

    def action_accept(self) -> None:
        selected = self._selection()
        if selected is None or selected[0] != "rejected" or not (row := self._track(selected)):
            return
        if not (rejection := self.snapshot.rejected[row.id]):
            self.notify("No rejected file to accept yet")
            return

        def accept(confirmed: bool | None) -> None:
            if confirmed:
                self._write(
                    row, lambda: review.accept(self.conn, row.id, rejection, self.cfg.library_root)
                )

        self.push_screen(
            Details(
                "Accept rejected audio?",
                f"Plan: {row.release} / {row.title}\nRecording: {row.recording}\n"
                f"Duration: {row.duration_ms if row.duration_ms is not None else 'unknown'} ms\n\n"
                f"AcoustID: {rejection.reason}\n"
                f"{rejection.comparison or 'No recording match details'}\n\n"
                f"Staged: {rejection.path}",
                confirm=True,
            ),
            accept,
        )

    def action_link(self) -> None:
        if not (row := self._unresolved()):
            return

        def link(value: str | None) -> None:
            if value is None:
                return
            try:
                parsed = parse_add_target(value)
                if not isinstance(parsed, MbLink):
                    raise ValueError("Expected a MusicBrainz link")
            except ValueError as exc:
                self.notify(str(exc), severity="warning")
                return
            if (
                bind(
                    self.conn,
                    row.id,
                    parsed,
                    self.cfg.plan.discography_limit,
                    expected=(row.origin, row.ref),
                )
                is None
            ):
                self.notify("already changed by the runner", severity="warning")
            self.refresh_dashboard()

        self.push_screen(Paste("MusicBrainz link"), link)
