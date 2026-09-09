"""Cancellation token shared by the agent loop, tools, and subprocesses."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable

from rinari.shared.errors import CancelledError


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = False
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a listener fired once cancel() runs (§8/Etapa D).

        Listeners are for fast local teardown (close a stream, abort a
        waiter, escalate a subprocess). Listener errors never propagate:
        cancellation must be reliable even with a broken listener.
        """
        with self._lock:
            cancelled = self._cancelled
            if not cancelled:
                self._callbacks.append(callback)
        if cancelled:
            self._fire(callback)
        return lambda: self.remove_callback(callback)

    def remove_callback(self, callback: Callable[[], None]) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self._callbacks.remove(callback)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            self._fire(callback)

    @staticmethod
    def _fire(callback: Callable[[], None]) -> None:
        with contextlib.suppress(Exception):
            callback()

    def reset(self) -> None:
        with self._lock:
            self._cancelled = False

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def throw_if_cancelled(self, message: str = "Operation cancelled") -> None:
        if self._cancelled:
            raise CancelledError(message, hint="The turn was interrupted.")
