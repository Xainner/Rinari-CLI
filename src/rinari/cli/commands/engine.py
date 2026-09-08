"""Machine transport for desktop clients: ``rinari engine --stdio`` (Phase 1).

Stdout is protocol-only NDJSON. Nothing here may print banners or Rich
output; diagnostics go to stderr via the transport.
"""

from __future__ import annotations

import typer

from rinari.cli import deps
from rinari.cli.deps import with_error_handling
from rinari.engine_protocol.server import EngineServer
from rinari.engine_protocol.transports.stdio import run_stdio
from rinari.shared.errors import InvalidUsageError


@with_error_handling("engine")
def engine(
    ctx: typer.Context,
    stdio: bool = typer.Option(
        False,
        "--stdio",
        help="Run the NDJSON engine protocol on stdin/stdout.",
    ),
) -> None:
    if not stdio:
        raise InvalidUsageError(
            "The engine transport requires --stdio.",
            hint="Run: rinari engine --stdio",
        )
    with deps.services(ctx) as services:
        code = run_stdio(EngineServer(services))
    raise typer.Exit(code)
