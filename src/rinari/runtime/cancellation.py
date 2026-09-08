"""Cancellation token shared by the agent loop, tools, and subprocesses."""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from rinari.shared.errors import CancelledError


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = False
        self._callbacks: list[Callable[[], None]] = []

    def on_cancel(self, callback: Callable[[], None]) -> None:
        """Register a listener fired once cancel() runs (§8/Etapa D).

        Listeners are for fast local teardown (close a stream, abort a
        waiter, escalate a subprocess). Listener errors never propagate:
        cancellation must be reliable even with a broken listener.
        """
        if self._cancelled:
            self._fire(callback)
            return
        self._callbacks.append(callback)

    def cancel(self) -> None:
        self._cancelled = True
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            self._fire(callback)

    @staticmethod
    def _fire(callback: Callable[[], None]) -> None:
        with contextlib.suppress(Exception):
            callback()

    def reset(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def throw_if_cancelled(self, message: str = "Operation cancelled") -> None:
        if self._cancelled:
            raise CancelledError(message, hint="The turn was interrupted.")
