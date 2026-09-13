"""CLI → Rinari Agent handoff: ``rinari desktop [path]`` (``code`` alias).

Opens the desktop client on the same project/session. The desktop binary
is resolved via RINARI_AGENT_BIN or PATH; legacy names remain supported. The engine never
guesses install locations. Target project/session travel as explicit CLI
args so a running single-instance window can route them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import typer

from rinari.cli.deps import with_error_handling
from rinari.shared.errors import InvalidUsageError


def _resolve_binary(explicit: str | None) -> str:
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return str(candidate)
        raise InvalidUsageError(
            f"Desktop binary not found: {explicit!r}.",
            hint="Set RINARI_AGENT_BIN to the rinari-agent executable.",
        )
    from_env = os.environ.get("RINARI_AGENT_BIN") or os.environ.get("RINARI_CODE_BIN")
    if from_env:
        return _resolve_binary(from_env)
    found = shutil.which("rinari-agent") or shutil.which("rinari-code")
    if found:
        return found
    raise InvalidUsageError(
        "Desktop client not found.",
        hint="Install Rinari Agent or set RINARI_AGENT_BIN.",
    )


@with_error_handling("desktop")
def code(
    ctx: typer.Context,
    path: str | None = typer.Argument(
        None, help="Project folder to open (default: current directory)."
    ),
    session: str | None = typer.Option(
        None, "--session", help="Resume this session id in the desktop client."
    ),
    binary: str | None = typer.Option(
        None, "--binary", help="Override the rinari-agent executable."
    ),
) -> None:
    target = Path(path or ".").resolve()
    if not target.is_dir():
        raise InvalidUsageError(f"Not a directory: {target}.")
    exe = _resolve_binary(binary)
    args = [exe, "--project", str(target)]
    if session:
        args += ["--session", session]
    try:
        subprocess.Popen(args, close_fds=True)
    except OSError as exc:
        raise InvalidUsageError(f"Could not launch Rinari Agent: {exc}") from exc
    typer.echo(f"Opening Rinari Agent: {target}")
