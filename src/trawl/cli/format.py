from rich.console import Console
from rich.table import Table
from rich.text import Text

from trawl.db import queue


def source_report(
    console: Console,
    outcomes: list[tuple[str, int, int]],
    pauses: list[queue.Pause],
) -> None:
    summary = Table("Source", "Accepted", "Rejected", title="Attempts")
    for source, accepted, rejected in outcomes:
        summary.add_row(source, str(accepted), str(rejected))
    console.print(summary)
    for pause in pauses:
        console.print(Text(f"Wall: {pause.source} until {pause.until} | {pause.reason}"))
