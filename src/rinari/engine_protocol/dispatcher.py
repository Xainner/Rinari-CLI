"""Request dispatcher: envelopes, duplicate IDs, unknown methods, error mapping."""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rinari.engine_protocol import errors
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.messages import failure, success
from rinari.shared.errors import RinariError

Handler = Callable[[dict[str, Any]], dict[str, Any]]

_ID_RE = re.compile(r'"id"\s*:\s*"([^"]+)"')
_log = logging.getLogger(__name__)
# A request that holds the request thread this long delays every other one
# (plan 0.4.0 #53): its name and duration go to the log, never its params.
SLOW_REQUEST_S = 3.0
#: A request still running after this long gets the other threads' stacks in
#: the log, once: what holds the request thread back (the database lock, the
#: GIL) is in another thread, and a listing of 35 ms took 4 s in the app with
#: nothing to say why (23-oct).
STACKS_AFTER_S = 2.0


def _stacks(skip: int) -> str:
    """The innermost frames of every thread but `skip`, a few lines each."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    out: list[str] = []
    for ident, frame in sys._current_frames().items():
        if ident in (skip, threading.get_ident()):
            continue
        frames = traceback.extract_stack(frame)
        # Rinari's own frames say what the thread is doing; the stdlib's
        # innermost ones (a wait, a socket read) only how.
        own = [
            f
            for f in frames
            if "rinari" in f.filename.replace("\\", "/").split("/site-packages/")[-1]
        ]
        shown = (own or frames)[-3:]
        where = " < ".join(
            f"{f.name} ({Path(f.filename).name}:{f.lineno})" for f in reversed(shown)
        )
        out.append(f"  {names.get(ident, ident)}: {where}")
    return "\n".join(out)


class _Watch:
    """Logs the other threads' stacks when a request runs too long."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: tuple[str, float, int] | None = None
        self._thread: threading.Thread | None = None

    def begin(self, method: str) -> None:
        with self._lock:
            self._current = (method, time.monotonic(), threading.get_ident())
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="rinari-slow-request-watch", daemon=True
                )
                self._thread.start()

    def end(self) -> None:
        with self._lock:
            self._current = None

    def _run(self) -> None:
        dumped: tuple[str, float, int] | None = None
        while True:
            time.sleep(0.5)
            with self._lock:
                current = self._current
            if current is None or current is dumped:
                continue
            method, started, ident = current
            if time.monotonic() - started >= STACKS_AFTER_S:
                dumped = current
                _log.warning(
                    "Request %s still running after %.1fs; other threads:\n%s",
                    method,
                    STACKS_AFTER_S,
                    _stacks(ident),
                )


def _extract_id(line: str) -> Any:
    match = _ID_RE.search(line)
    return match.group(1) if match else None


class EngineDispatcher:
    """Routes one NDJSON line to a registered handler.

    Duplicate request IDs are rejected without executing the handler.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._seen_ids: set[Any] = set()
        self._watch = _Watch()

    def register(self, method: str, handler: Handler) -> None:
        self._handlers[method] = handler

    def handler(self, method: str) -> Handler | None:
        """A registered method, for another method that reuses it."""
        return self._handlers.get(method)

    def dispatch(self, line: str) -> dict[str, Any] | None:
        if line.strip() == "":
            return None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return failure(_extract_id(line), errors.BROKEN_FRAME, "Request is not valid JSON.")
        if not isinstance(obj, dict):
            return failure(None, errors.MALFORMED_REQUEST, "Request must be a JSON object.")
        request_id = obj.get("id")
        if not isinstance(request_id, (str, int)) or request_id == "":
            return failure(
                None, errors.MALFORMED_REQUEST, "Request 'id' must be a non-empty string."
            )
        method = obj.get("method")
        if not isinstance(method, str) or not method:
            return failure(
                request_id, errors.MALFORMED_REQUEST, "Request 'method' must be a string."
            )
        params = obj.get("params", {})
        if not isinstance(params, dict):
            return failure(request_id, errors.INVALID_PARAMS, "Request 'params' must be an object.")
        if request_id in self._seen_ids:
            return failure(
                request_id,
                errors.DUPLICATE_REQUEST_ID,
                f"Duplicate request id: {request_id!r}.",
            )
        handler = self._handlers.get(method)
        if handler is None:
            return failure(
                request_id,
                errors.UNKNOWN_METHOD,
                f"Unknown method: {method}.",
                details={"method": method},
            )
        self._seen_ids.add(request_id)
        started = time.monotonic()
        self._watch.begin(method)
        try:
            return success(request_id, handler(params))
        except EngineProtocolError as err:
            return failure(request_id, err.code, err.message, err.retryable, err.details)
        except RinariError as err:
            mapped = errors.from_rinari_error(err)
            return failure(
                request_id, mapped.code, mapped.message, mapped.retryable, mapped.details
            )
        except Exception as err:  # defensive: never break the stdio frame stream
            return failure(request_id, errors.ENGINE_ERROR, f"{type(err).__name__}: {err}")
        finally:
            self._watch.end()
            elapsed = time.monotonic() - started
            if elapsed >= SLOW_REQUEST_S:
                _log.warning("Slow request on the request thread: %s took %.1fs", method, elapsed)
