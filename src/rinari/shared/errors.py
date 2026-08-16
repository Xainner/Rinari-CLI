"""Structured error classes mapped to stable CLI exit codes.

Exit codes follow docs/commands.md section 68; they are part of the
public contract and must not be renumbered casually.
"""

from __future__ import annotations

from enum import IntEnum
from typing import ClassVar


class ExitCode(IntEnum):
    SUCCESS = 0
    GENERIC_FAILURE = 1
    INVALID_USAGE = 2
    CONFIGURATION_ERROR = 3
    AUTHENTICATION_REQUIRED = 4
    PERMISSION_DENIED = 5
    APPROVAL_DENIED = 6
    NOT_FOUND = 7
    CONFLICT = 8
    VALIDATION_FAILED = 9
    NETWORK_FAILURE = 10
    PROVIDER_MODEL_FAILURE = 11
    TOOL_FAILURE = 12
    CANCELLED = 13
    PARTIAL_COMPLETION = 14
    BLOCKED = 15


class RinariError(Exception):
    exit_code: ClassVar[ExitCode] = ExitCode.GENERIC_FAILURE

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    @property
    def code(self) -> ExitCode:
        return type(self).exit_code


class InvalidUsageError(RinariError):
    exit_code = ExitCode.INVALID_USAGE


class ConfigurationError(RinariError):
    exit_code = ExitCode.CONFIGURATION_ERROR


class AuthenticationRequiredError(RinariError):
    exit_code = ExitCode.AUTHENTICATION_REQUIRED


class PermissionDeniedError(RinariError):
    exit_code = ExitCode.PERMISSION_DENIED


class ApprovalDeniedError(RinariError):
    exit_code = ExitCode.APPROVAL_DENIED


class NotFoundError(RinariError):
    exit_code = ExitCode.NOT_FOUND


class ConflictError(RinariError):
    exit_code = ExitCode.CONFLICT


class ValidationFailureError(RinariError):
    exit_code = ExitCode.VALIDATION_FAILED


class NetworkError(RinariError):
    exit_code = ExitCode.NETWORK_FAILURE


class ProviderModelError(RinariError):
    exit_code = ExitCode.PROVIDER_MODEL_FAILURE


class ToolError(RinariError):
    exit_code = ExitCode.TOOL_FAILURE


class CancelledError(RinariError):
    exit_code = ExitCode.CANCELLED


class PartialCompletionError(RinariError):
    exit_code = ExitCode.PARTIAL_COMPLETION


class BlockedError(RinariError):
    exit_code = ExitCode.BLOCKED
