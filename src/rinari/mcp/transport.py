"""MCP transports: the channel under the wire (stdio in v1).

- `StdioTransport`: spawns the server as a subprocess and speaks
  newline-delimited JSON-RPC 2.0 on its stdin/stdout. A reader thread pumps
  stdout frames into a queue; requests are matched by JSON-RPC id.
- `InProcessTransport`: an in-memory channel around a synchronous handler
  callable. Used by tests (and future embedded servers) so the protocol,
  client and adapter are testable without spawning processes.

Both satisfy the `McpTransport` contract: start / send / close.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from .protocol import McpMessage, parse_message


class TransportError(Exception):
    """Structured transport failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        # TRANSPORT_START | TRANSPORT_CLOSED | TRANSPORT_TIMEOUT | TRANSPORT_IO
        self.code = code
        self.message = message


class McpTransport:
    """Channel contract for the MCP client."""

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
        self._queue: queue.Queue[str] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False

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
                self._queue.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._queue.put("")  # EOF sentinel

    def send(self, payload: str, timeout_s: float) -> McpMessage:
        if self._closed:
            raise TransportError("TRANSPORT_CLOSED", "transport is closed")
        with self._lock:
            assert self._process is not None and self._process.stdin is not None
            try:
                self._process.stdin.write(payload + "\n")
                self._process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise TransportError("TRANSPORT_IO", f"write failed: {exc}") from exc
        return self._wait_for_response(payload, timeout_s)

    def _wait_for_response(self, payload: str, timeout_s: float) -> McpMessage:
        match = re.search(r'"id"\s*:\s*(\d+)', payload)
        expected = int(match.group(1)) if match else None
        while True:
            try:
                line = self._queue.get(timeout=timeout_s)
            except queue.Empty as exc:
                raise TransportError(
                    "TRANSPORT_TIMEOUT", f"server did not answer within {timeout_s}s"
                ) from exc
            if line == "":  # EOF: server went away
                raise TransportError("TRANSPORT_IO", "server closed the connection")
            message = parse_message(line)
            if message is None:
                continue
            if expected is None:
                # No id in request (should not happen for requests): accept first frame.
                return message
            if message.is_response and message.id == expected:
                return message
            # Otherwise: notification/event from the server -- ignore.

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

    def close(self) -> None:
        self._closed = True


__all__ = ["InProcessTransport", "McpTransport", "StdioTransport", "TransportError"]
