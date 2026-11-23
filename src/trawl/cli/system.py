import asyncio
from enum import StrEnum
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from trawl.cli import doctor
from trawl.cli.context import Options, fail
from trawl.config.load import load
from trawl.config.models import ConfigError
from trawl.runner.service import service as manage_service


def doctor_command(
    ctx: typer.Context, offline: Annotated[bool, typer.Option("--offline")] = False
) -> None:
    """Check configuration, directories, schema, tools, and MusicBrainz."""
    opts: Options = ctx.obj
    checks = asyncio.run(doctor.check(opts.config, offline))
    table = Table("Check", "Status", "Detail")
    for result in checks:
        table.add_row(result.name, result.status, Text(result.detail))
    Console().print(table)
    if any(result.status == "FAIL" for result in checks):
        raise typer.Exit(1)


class ServiceAction(StrEnum):
    INSTALL = "install"
    UNINSTALL = "uninstall"
    STATUS = "status"


def service(ctx: typer.Context, action: ServiceAction) -> None:
    """Manage the background watch service."""
    opts: Options = ctx.obj
    try:
        if action == ServiceAction.INSTALL:
            load(opts.config)
        code = manage_service(action, opts.config)
    except (ConfigError, OSError) as exc:
        fail(str(exc), 1)
    if code:
        raise typer.Exit(code)
