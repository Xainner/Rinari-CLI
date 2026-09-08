"""NDJSON stdio transport: stdin requests, stdout responses, stderr diagnostics.

Rules (Rinari Code §6.2): one JSON object per line, stdout is protocol-only
(no banners, no ANSI), every response references a request id, async events
carry session/turn identifiers.
"""

from __future__ import annotations

import json
import sys
from typing import TextIO

from rinari.engine_protocol.server import EngineServer


def run_stdio(
    server: EngineServer,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    out.write(json.dumps(server.hello()) + "\n")
    out.flush()
    while True:
        try:
            line = inp.readline()
        except KeyboardInterrupt:
            return 0
        if line == "":
            return 0  # EOF: desktop closed the pipe
        try:
            response = server.handle_line(line)
        except Exception as exc:  # defensive: keep serving further requests
            print(f"engine: internal dispatch failure: {type(exc).__name__}: {exc}", file=err)
            continue
        if response is None:
            continue
        try:
            out.write(json.dumps(response) + "\n")
            out.flush()
        except BrokenPipeError:
            return 0
    return 0
