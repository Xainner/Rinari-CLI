"""CDP (Chrome DevTools Protocol) session (phase 5, "Browser engine" decision).

JSON-RPC over the RFC6455 client:

- command:  {"id": n, "method": ..., "params": {...}}
            -> {"id": n, "result": {...}} | {"id": n, "error": {...}}
- event:    pushed by the browser, {"method": ..., "params": {...}}
- flattened sessions: Target.attachToTarget(flatten=True) yields a
  `sessionId`; commands carry that field and events for that target arrive
  tagged with it, all over the same websocket.

A background reader thread dispatches responses to per-id waiters and
events into a bounded queue; `send()` blocks until its response or a
timeout. Cancellation is the caller's job (deadlines + token between ops).
"""

from __future__ import annotations

import contextlib
import itertools
import json
import queue
import threading
import time
from typing import Any

from rinari.browser.ws import WebSocketClient, WsError

DEFAULT_COMMAND_TIMEOUT_S = 15.0
EVENT_QUEUE_LIMIT = 2048


class CdpError(Exception):
    """Structured CDP failure (maps to browser tool error codes)."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code  # CDP_ERROR | CDP_TIMEOUT | CDP_DISCONNECTED | CDP_CLOSED
        self.message = message
        self.retryable = retryable


class CdpSession:
    def __init__(
        self,
        ws_url: str,
        *,
        timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
        ws_client: WebSocketClient | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._timeout_s = timeout_s
        self._ws = ws_client if ws_client is not None else WebSocketClient(timeout_s=timeout_s)
        self._ids = itertools.count(1)
        self._waiters: dict[int, queue.Queue] = {}
        self._events: queue.Queue[dict] = queue.Queue(maxsize=EVENT_QUEUE_LIMIT)
        self._fatal: Exception | None = None
        self._reader: threading.Thread | None = None
        self._closed = False

    @property
    def fatal(self) -> CdpError | None:
        return None if self._fatal is None else _wrap_fatal(self._fatal)

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        self._ws.connect(self._ws_url)
        self._reader = threading.Thread(target=self._reader_loop, name="rinari-cdp", daemon=True)
        self._reader.start()

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        if self._fatal is not None:
            raise _wrap_fatal(self._fatal)
        if self._closed:
            raise CdpError("CDP_CLOSED", "CDP session is closed")
        request_id = next(self._ids)
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params:
            message["params"] = params
        if session_id is not None:
            message["sessionId"] = session_id
        waiter: queue.Queue = queue.Queue(maxsize=1)
        self._waiters[request_id] = waiter
        try:
            self._ws.send(json.dumps(message, separators=(",", ":")))
        except WsError as exc:
            raise _wrap_fatal(exc) from exc
        try:
            outcome = waiter.get(timeout=timeout_s or self._timeout_s)
        except queue.Empty:
            raise CdpError("CDP_TIMEOUT", f"{method} timed out", retryable=True) from None
        finally:
            self._waiters.pop(request_id, None)
        kind, payload = outcome
        if kind == "fatal":
            raise payload if isinstance(payload, CdpError) else _wrap_fatal(payload)
        response = payload
        if "error" in response:
            err = response["error"]
            raise CdpError(
                "CDP_ERROR",
                f"{method}: {err.get('message', 'unknown error')} (code {err.get('code')})",
            )
        return response.get("result", {})

    def events(
        self,
        session_id: str | None = None,
        *,
        methods: set[str] | None = None,
        limit: int = 100,
        wait_s: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Drain queued events, optionally filtered by flattened session and method.

        Non-matching events are re-queued so one reader cannot consume
        another tool's event stream. When `wait_s` is positive, an empty
        drain polls briefly (events are pushed asynchronously after the
        command that enables them).
        """
        deadline = time.monotonic() + wait_s if wait_s and wait_s > 0 else None
        while True:
            drained = self._drain_events_once(session_id, methods, limit)
            if drained or deadline is None or time.monotonic() >= deadline:
                return drained
            time.sleep(0.02)

    def _drain_events_once(
        self,
        session_id: str | None,
        methods: set[str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        drained: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        def requeue() -> None:
            for stored in skipped:
                with contextlib.suppress(queue.Full):
                    self._events.put_nowait(stored)
            skipped.clear()

        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                requeue()
                return drained
            if session_id is not None and event.get("sessionId") != session_id:
                skipped.append(event)
                continue
            if methods is not None and event.get("method") not in methods:
                skipped.append(event)
                continue
            drained.append(event)
            if len(drained) >= max(limit, 1):
                requeue()
                return drained

    def close(self) -> None:
        self._closed = True
        self._ws.close()
        reader = self._reader
        if reader is not None:
            reader.join(timeout=2.0)
            self._reader = None

    # -- internals ------------------------------------------------------------

    def _reader_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    raw = self._ws.recv(timeout_s=1.0)
                except WsError as exc:
                    self._fail(exc)
                    return
                if raw is None:
                    self._fail(WsError("WS_CLOSED", "closed by the browser"))
                    return
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue  # CDP is strict JSON; a malformed frame is dropped
                if "id" in message:
                    waiter = self._waiters.pop(message["id"], None)
                    if waiter is not None:
                        waiter.put_nowait(("response", message))
                else:
                    # Bounded queue: when full, drop the new event rather than
                    # block the reader thread.
                    with contextlib.suppress(queue.Full):
                        self._events.put_nowait(message)
        except Exception as exc:  # defensive: the session must not die silently
            self._fail(exc)

    def _fail(self, error: Exception) -> None:
        self._fatal = _wrap_fatal(error)
        for waiter in list(self._waiters.values()):
            with contextlib.suppress(queue.Full):
                waiter.put_nowait(("fatal", self._fatal))
        self._waiters.clear()


def _wrap_fatal(error: Exception) -> CdpError:
    if isinstance(error, CdpError):
        return error
    if isinstance(error, WsError):
        if error.code == "WS_TIMEOUT":
            return CdpError("CDP_TIMEOUT", error.message, retryable=True)
        return CdpError(
            "CDP_DISCONNECTED", f"CDP connection lost: {error.message}", retryable=error.retryable
        )
    return CdpError("CDP_DISCONNECTED", f"{error.__class__.__name__}: {error}")


__all__ = ["DEFAULT_COMMAND_TIMEOUT_S", "EVENT_QUEUE_LIMIT", "CdpError", "CdpSession"]
