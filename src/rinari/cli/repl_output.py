"""Live process output rendering for the REPL (kept apart to avoid a
repl <-> agent_runtime import cycle)."""

from __future__ import annotations

import sys


def emit(stream: str, chunk: str) -> None:
    for line in chunk.splitlines(keepends=True):
        if stream == "stderr":
            sys.stderr.write(f"│ ! {line}")
        else:
            sys.stdout.write(f"│   {line}")
    if stream == "stderr":
        sys.stderr.flush()
    else:
        sys.stdout.flush()
