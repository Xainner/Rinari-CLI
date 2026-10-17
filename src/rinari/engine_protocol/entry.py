"""Run the Engine on stdio: shared by ``rinari engine --stdio`` and the fast entry.

The desktop starts the Engine with ``python -m rinari.engine_protocol``
(plan 0.4.0 #91): the same server without Typer and the CLI's ~43 commands,
which cost about a second before the hello on every start.

Stdout is protocol-only NDJSON. Nothing here may print banners or Rich
output; diagnostics go to stderr via the transport.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

#: Held for the Engine's whole life: two Engines on one home would both run
#: scheduled tasks and resumes and overwrite each other's files (for example
#: Rinari Agent and Rinari Agent Nightly sharing ~/.rinari).
ENGINE_LOCK = "engine.lock"


def serve(config_path: Path | None = None) -> int:
    """Serve the protocol until the client closes stdin; returns the exit code."""
    from rinari.application.context import build_app_context
    from rinari.application.services import build_services
    from rinari.engine_protocol.server import EngineServer
    from rinari.engine_protocol.transports.stdio import run_stdio
    from rinari.net import shared_client
    from rinari.shared.errors import LockTimeoutError
    from rinari.shared.locking import file_lock
    from rinari.shared.paths import resolve_home

    _configure_logging()
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
        context = build_app_context(config_path=config_path)
        stack.callback(context.close)
        # Follows ~/.rinari/network.json while the Engine runs (plan 0.4.0 #96).
        client = shared_client(context.home, timeout=10.0)
        stack.callback(client.close)
        services = build_services(context, http_client=client, user_home=Path.home())
        server = EngineServer(services)
        server.start_background()
        try:
            return run_stdio(server)
        finally:
            # The desktop closed or the pipe broke: stop what the model left
            # running (#90). It used to outlive every Engine as an orphan.
            server.stop_owned_processes()


def _configure_logging() -> None:
    """«Registros detallados» (#96): the desktop sets RINARI_LOG_LEVEL for 24 h.

    Without it Python keeps its default (warnings and errors to stderr, which
    the desktop writes to engine.log).
    """
    import logging
    import os
    import sys

    level = os.environ.get("RINARI_LOG_LEVEL", "").strip().upper()
    if level in {"DEBUG", "INFO"}:
        logging.basicConfig(
            level=getattr(logging, level),
            stream=sys.stderr,
            format="%(levelname)s %(name)s: %(message)s",
        )
