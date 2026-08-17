"""Cancellation token shared by the agent loop, tools, and subprocesses."""

from __future__ import annotations

from rinari.shared.errors import CancelledError


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def reset(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def throw_if_cancelled(self, message: str = "Operation cancelled") -> None:
        if self._cancelled:
            raise CancelledError(message, hint="The turn was interrupted.")
