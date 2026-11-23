from typing import ClassVar, cast

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Label, Static

from trawl.db.review import Choice


class Grid(DataTable[Text]):
    def __init__(self, identifier: str) -> None:
        super().__init__(id=identifier, cursor_type="row", zebra_stripes=True)

    def append(self, key: str, *cells: str) -> None:
        self.add_row(*(Text(cell) for cell in cells), key=key)


class Details(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Back"),
        Binding("enter", "confirm", "Confirm"),
    ]

    def __init__(self, title: str, body: str, *, confirm: bool = False) -> None:
        super().__init__()
        self.heading = title
        self.body = body
        self.confirm = confirm

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.heading, classes="heading", markup=False)
            with VerticalScroll():
                yield Static(self.body, id="detail", markup=False)
            yield Label("Enter accept · Esc cancel" if self.confirm else "Esc close")

    def action_close(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(self.confirm)


class Paste(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "Cancel")]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog paste"):
            yield Label(self.prompt, classes="heading", markup=False)
            yield Input(placeholder=self.prompt)
            yield Label("Enter submit · Esc cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        if value := event.value.strip():
            self.dismiss(value)

    def action_close(self) -> None:
        self.dismiss(None)


class Candidates(ModalScreen[Choice | str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Cancel"),
        Binding("u", "paste", "Paste URL or path"),
    ]

    def __init__(self, track: str, choices: list[Choice]) -> None:
        super().__init__()
        self.track = track
        self.choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"Candidates · {self.track}", classes="heading", markup=False)
            yield Grid("candidates")
            yield Label("Enter pin · u paste YouTube URL or local path · Esc cancel")

    def on_mount(self) -> None:
        grid = self.query_one(Grid)
        grid.add_columns("Source", "Candidate", "Score", "Last result")
        for index, choice in enumerate(self.choices):
            origin = (
                f"{choice.ref}: {choice.item_ref}" if choice.ref != choice.item_ref else choice.ref
            )
            grid.append(
                str(index),
                choice.source,
                f"{choice.title} [{origin}]",
                f"{choice.score:.2f}",
                choice.reason or "Not tried",
            )
        grid.focus()

    @on(DataTable.RowSelected, "#candidates")
    def pick(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is not None:
            self.dismiss(self.choices[int(event.row_key.value)])

    def action_close(self) -> None:
        self.dismiss(None)

    def action_paste(self) -> None:
        def pin(value: str | None) -> None:
            if value is not None:
                self.dismiss(value)

        dashboard = cast("App[object]", self.app)  # pyright: ignore[reportUnknownMemberType]
        dashboard.push_screen(Paste("YouTube URL or local path"), pin)
