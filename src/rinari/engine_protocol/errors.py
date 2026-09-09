"""Engine Protocol errors: stable machine codes for the desktop client."""

from __future__ import annotations

from typing import Any

from rinari.shared.errors import RinariError

UNKNOWN_METHOD = "UNKNOWN_METHOD"
DUPLICATE_REQUEST_ID = "DUPLICATE_REQUEST_ID"
MALFORMED_REQUEST = "MALFORMED_REQUEST"
BROKEN_FRAME = "BROKEN_FRAME"
INVALID_PARAMS = "INVALID_PARAMS"
PTY_UNSUPPORTED = "PTY_UNSUPPORTED"
TURN_RUNNING = "TURN_RUNNING"
NO_ACTIVE_TURN = "NO_ACTIVE_TURN"
APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
SESSION_CLOSED = "SESSION_CLOSED"
ENGINE_ERROR = "ENGINE_ERROR"
TURN_PREPARATION_TIMEOUT = "TURN_PREPARATION_TIMEOUT"


class EngineProtocolError(Exception):
    """A protocol-level failure with a stable machine code."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


def from_rinari_error(err: RinariError) -> EngineProtocolError:
    """Map harness errors to protocol errors via their stable machine codes."""
    details: dict[str, Any] = {}
    if getattr(err, "hint", None):
        details["hint"] = err.hint
    return EngineProtocolError(
        code=err.machine_code,
        message=err.message,
        retryable=bool(getattr(err, "retryable", False)),
        details=details,
    )
