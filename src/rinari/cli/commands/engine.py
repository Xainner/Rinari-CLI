"""Machine transport for desktop clients: ``rinari engine --stdio`` (Phase 1).

Stdout is protocol-only NDJSON. Nothing here may print banners or Rich
output; diagnostics go to stderr via the transport. The desktop uses the
fast entry ``python -m rinari.engine_protocol`` (same server, no Typer).
"""

from __future__ import annotations

import typer

from rinari.cli.deps import get_params, with_error_handling
from rinari.engine_protocol.entry import ENGINE_LOCK, serve
from rinari.shared.errors import InvalidUsageError

__all__ = ["ENGINE_LOCK", "engine"]


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
    raise typer.Exit(serve(get_params(ctx).config))
