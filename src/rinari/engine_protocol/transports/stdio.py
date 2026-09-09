"""NDJSON stdio transport: stdin requests, stdout responses/events, stderr diagnostics.

Rules (Rinari Code §6.2): one JSON object per line, stdout is protocol-only
(no banners, no ANSI), every response references a request id, async events
carry session/turn identifiers.

Turns run in worker threads while this loop keeps reading stdin, so
`session.turn.cancel` and `approval.resolve` arrive mid-turn. A dedicated
reader thread feeds requests; the main loop dispatches them and drains the
event outbox with bounded latency (TurnManager.EVENT_POLL_S).
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from typing import Any, TextIO

from rinari.engine_protocol.server import EngineServer

EVENT_POLL_S = 0.05
# Requests slower than this get one stderr diagnostic line (desktop log
# shows them; stdout stays protocol-only). Helps catch stalls like a
# network-blocked call holding up later requests on the single loop.
SLOW_REQUEST_S = 10.0
# After stdin closes, keep draining until in-flight turns settle (bounded:
# an approval-blocked turn still ends at its own timeout, then we leave).
EOF_DRAIN_WAIT_S = 60.0

_EOF = object()


def _reader(inp: TextIO, requests: queue.Queue) -> None:
    try:
        for line in inp:
            requests.put(line)
    finally:
        requests.put(_EOF)


def _emit(out: TextIO, payload: dict[str, Any]) -> bool:
    try:
        out.write(json.dumps(payload) + "\n")
        out.flush()
    except BrokenPipeError:
        return False
    return True


def run_stdio(
    server: EngineServer,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    if not _emit(out, server.hello()):
        return 0
    requests: queue.Queue = queue.Queue()
    thread = threading.Thread(target=_reader, args=(inp, requests), daemon=True)
    thread.start()
    eof = False
    eof_since = 0.0
    while True:
        if not eof:
            try:
                item = requests.get(timeout=EVENT_POLL_S)
            except queue.Empty:
                item = None
            if item is _EOF:
                eof = True
                eof_since = time.monotonic()
            elif item is not None:
                started = time.monotonic()
                try:
                    response = server.handle_line(item)
                except Exception as exc:  # defensive: keep serving further requests
                    print(
                        f"engine: internal dispatch failure: {type(exc).__name__}: {exc}",
                        file=err,
                    )
                    continue
                elapsed = time.monotonic() - started
                if elapsed > SLOW_REQUEST_S:
                    print(
                        f"engine: slow request {elapsed:.1f}s: {item[:120]}",
                        file=err,
                    )
                if response is not None and not _emit(out, response):
                    return 0
        drained_any = False
        for pending in server.drain_events():
            drained_any = True
            if not _emit(out, pending):
                return 0
        if eof:
            # Stdin closed: wait for in-flight turns to settle so the desktop
            # never loses a turn that was accepted but not yet flushed.
            # Quiescence needs drain-empty *then* inactive: workers always
            # emit the terminal event before marking done, so this ordering
            # cannot skip an event emitted before we looked. Daemon workers
            # die with the process if the deadline hits.
            if not drained_any and not server.has_active_turns():
                late = server.drain_events()
                if not late and not server.has_active_turns():
                    return 0
                for pending in late:
                    if not _emit(out, pending):
                        return 0
            if time.monotonic() - eof_since > EOF_DRAIN_WAIT_S:
                return 0
            time.sleep(EVENT_POLL_S)
