"""MCP transports: the channel under the wire.

- `StdioTransport`: spawns the server as a subprocess and speaks
  newline-delimited JSON-RPC 2.0 on its stdin/stdout. A reader thread routes
  each response to the request waiting for its JSON-RPC id, so two sessions
  can call the same server at once, and hands the notifications the server
  sends on its own (`notifications/tools/list_changed`…) to `on_notification`.
- `InProcessTransport`: an in-memory channel around a synchronous handler
  callable. Used by tests (and future embedded servers) so the protocol,
  client and adapter are testable without spawning processes.

All satisfy the `McpTransport` contract: start / send / notify / close, plus
the optional `on_notification` callback the client sets. The remote
Streamable HTTP transport lives in `http_transport.py`.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .protocol import McpMessage, parse_message

# A call that keeps reporting progress may wait this long in all.
MAX_PROGRESS_WAIT_S = 30 * 60.0
# Put in a waiting request's box when another frame arrives: renews the wait.
_TICK = object()


class TransportError(Exception):
    """Structured transport failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        # TRANSPORT_START | TRANSPORT_CLOSED | TRANSPORT_TIMEOUT | TRANSPORT_IO
        # HTTP: TRANSPORT_AUTH | TRANSPORT_UNREACHABLE | TRANSPORT_TLS
        # | TRANSPORT_NOT_FOUND | TRANSPORT_SESSION_EXPIRED | TRANSPORT_HTTP
        # | TRANSPORT_PROTOCOL | TRANSPORT_UNSUPPORTED
        self.code = code
        self.message = message
        self.http_status = http_status
        self.hint = hint


class McpTransport:
    """Channel contract for the MCP client."""

    #: Called with each notification the server sends on its own (no id). The
    #: client sets it; it runs on the transport's thread and must not block.
    on_notification: Callable[[McpMessage], None] | None = None

    def start(self) -> None: ...

    def send(self, payload: str, timeout_s: float) -> McpMessage: ...

    def notify(self, payload: str) -> None: ...

    def close(self) -> None: ...


class StdioTransport(McpTransport):
    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._command = list(command)
        self._cwd = cwd
        self._env = env
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False
        # Requests waiting for their answer, by JSON-RPC id. The reader puts
        # the response there, a tick for any other frame and None at EOF.
        self._pending: dict[Any, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._eof = False

    def start(self) -> None:
        if self._process is not None:
            return
        env = dict(os.environ)
        if self._env:
            env.update(self._env)
        try:
            self._process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self._cwd) if self._cwd else None,
                env=env,
                text=True,
                bufsize=1,
            )
        except (OSError, ValueError) as exc:
            raise TransportError(
                "TRANSPORT_START", f"cannot start {self._command[0]}: {exc}"
            ) from exc
        self._reader = threading.Thread(target=self._pump, name="mcp-stdio-reader", daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            for line in self._process.stdout:
                self._route(line)
        except (OSError, ValueError):
            pass
        finally:
            with self._pending_lock:
                self._eof = True
                waiting = list(self._pending.values())
            for box in waiting:
                box.put(None)  # the server went away

    def _route(self, line: str) -> None:
        message = parse_message(line)
        if message is None:
            return
        if message.is_response:
            with self._pending_lock:
                box = self._pending.get(message.id)
            if box is not None:
                box.put(message)
            return
        # Every other frame renews the waits, so progress notifications keep
        # a long call alive (the ceiling still bounds it).
        self._tick()
        if message.method and message.id is not None:
            self._answer_server_request(message)
            return
        callback = self.on_notification
        if message.method and callback is not None:
            with contextlib.suppress(Exception):  # a listener never breaks the reader
                callback(message)

    def _tick(self) -> None:
        with self._pending_lock:
            waiting = list(self._pending.values())
        for box in waiting:
            box.put(_TICK)

    def _answer_server_request(self, message: McpMessage) -> None:
        """`ping` gets an empty result; anything else was never advertised."""
        if message.method == "ping":
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message.id, "result": {}}
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": message.id,
                "error": {"code": -32601, "message": "method not supported by client"},
            }
        self.notify(json.dumps(reply, separators=(",", ":")))

    def send(self, payload: str, timeout_s: float) -> McpMessage:
        if self._closed:
            raise TransportError("TRANSPORT_CLOSED", "transport is closed")
        try:
            expected = json.loads(payload).get("id")
        except (ValueError, AttributeError) as exc:
            raise TransportError("TRANSPORT_IO", "invalid outgoing frame") from exc
        box: queue.Queue = queue.Queue()
        with self._pending_lock:
            if self._eof:
                raise TransportError("TRANSPORT_IO", "server closed the connection")
            self._pending[expected] = box
        try:
            with self._lock:
                assert self._process is not None and self._process.stdin is not None
                try:
                    self._process.stdin.write(payload + "\n")
                    self._process.stdin.flush()
                except (OSError, ValueError) as exc:
                    raise TransportError("TRANSPORT_IO", f"write failed: {exc}") from exc
            return self._wait_for_response(box, timeout_s)
        finally:
            with self._pending_lock:
                self._pending.pop(expected, None)

    def _wait_for_response(self, box: queue.Queue, timeout_s: float) -> McpMessage:
        # Every frame from the server restarts the wait, so progress
        # notifications keep a long call alive; the ceiling bounds it in all.
        ceiling = time.monotonic() + max(timeout_s, MAX_PROGRESS_WAIT_S)
        while True:
            if time.monotonic() > ceiling:
                raise TransportError(
                    "TRANSPORT_TIMEOUT", f"server did not answer within {MAX_PROGRESS_WAIT_S:g}s"
                )
            try:
                item = box.get(timeout=timeout_s)
            except queue.Empty as exc:
                raise TransportError(
                    "TRANSPORT_TIMEOUT", f"server did not answer within {timeout_s}s"
                ) from exc
            if item is None:
                raise TransportError("TRANSPORT_IO", "server closed the connection")
            if item is _TICK:
                continue
            return item

    def notify(self, payload: str) -> None:
        if self._closed:
            return
        with self._lock:
            if self._process is None or self._process.stdin is None:
                return
            try:
                self._process.stdin.write(payload + "\n")
                self._process.stdin.flush()
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        self._closed = True
        process = self._process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        self._process = None


class InProcessTransport(McpTransport):
    """In-memory transport: `handler(message) -> str | None` frame or None."""

    def __init__(self, handler: Callable[[str], str | None]) -> None:
        self._handler = handler
        self._closed = False

    def start(self) -> None:
        return None

    def send(self, payload: str, timeout_s: float) -> McpMessage:
        if self._closed:
            raise TransportError("TRANSPORT_CLOSED", "transport is closed")
        frame = self._handler(payload)
        if frame is None:
            raise TransportError("TRANSPORT_IO", "server produced no response")
        message = parse_message(frame)
        if message is None:
            raise TransportError("TRANSPORT_IO", "server frame is not valid JSON-RPC")
        return message

    def notify(self, payload: str) -> None:
        if self._closed:
            return
        self._handler(payload)

    def push(self, frame: str) -> None:
        """Deliver a notification from the server (tests, embedded servers)."""
        message = parse_message(frame)
        callback = self.on_notification
        if message is not None and callback is not None and not self._closed:
            callback(message)

    def close(self) -> None:
        self._closed = True


__all__ = ["InProcessTransport", "McpTransport", "StdioTransport", "TransportError"]
