"""Machine transport for desktop clients: ``rinari engine --stdio`` (Phase 1).

Stdout is protocol-only NDJSON. Nothing here may print banners or Rich
output; diagnostics go to stderr via the transport.
"""

from __future__ import annotations

from contextlib import ExitStack

import typer

from rinari.cli import deps
from rinari.cli.deps import with_error_handling
from rinari.engine_protocol.server import EngineServer
from rinari.engine_protocol.transports.stdio import run_stdio
from rinari.shared.errors import InvalidUsageError, LockTimeoutError
from rinari.shared.locking import file_lock
from rinari.shared.paths import resolve_home

#: Held for the Engine's whole life: two Engines on one home would both run
#: scheduled tasks and resumes and overwrite each other's files (for example
#: Rinari Agent and Rinari Agent Nightly sharing ~/.rinari).
ENGINE_LOCK = "engine.lock"


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
    with ExitStack() as stack:
        # Only this lock's timeout means "another Engine"; other locks inside
        # the Engine (credentials, sessions) keep their own errors.
        try:
            stack.enter_context(file_lock(resolve_home() / ENGINE_LOCK, timeout=3.0))
        except LockTimeoutError as exc:
            raise LockTimeoutError(
                f"Another Rinari engine is already using {resolve_home()}.",
                hint="Close the other Rinari Agent (or Rinari Agent Nightly), also from the tray.",
            ) from exc
        services = stack.enter_context(deps.services(ctx))
        server = EngineServer(services)
        server.start_background()
        code = run_stdio(server)
    raise typer.Exit(code)
