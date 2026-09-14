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
    machine_code: ClassVar[str] = "GENERIC_FAILURE"

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    @property
    def code(self) -> ExitCode:
        return type(self).exit_code

    @property
    def retryable(self) -> bool:
        return False


class InvalidUsageError(RinariError):
    exit_code = ExitCode.INVALID_USAGE
    machine_code = "INVALID_USAGE"


class ConfigurationError(RinariError):
    exit_code = ExitCode.CONFIGURATION_ERROR
    machine_code = "CONFIGURATION_ERROR"


class AuthenticationRequiredError(RinariError):
    exit_code = ExitCode.AUTHENTICATION_REQUIRED
    machine_code = "AUTHENTICATION_REQUIRED"


class PermissionDeniedError(RinariError):
    exit_code = ExitCode.PERMISSION_DENIED
    machine_code = "PERMISSION_DENIED"


class SandboxViolationError(RinariError):
    exit_code = ExitCode.PERMISSION_DENIED
    machine_code = "SANDBOX_VIOLATION"


class ApprovalDeniedError(RinariError):
    exit_code = ExitCode.APPROVAL_DENIED
    machine_code = "APPROVAL_DENIED"


class NotFoundError(RinariError):
    exit_code = ExitCode.NOT_FOUND
    machine_code = "NOT_FOUND"


class ConflictError(RinariError):
    exit_code = ExitCode.CONFLICT
    machine_code = "CONFLICT"


class ValidationFailureError(RinariError):
    exit_code = ExitCode.VALIDATION_FAILED
    machine_code = "VALIDATION_FAILED"


class NetworkError(RinariError):
    exit_code = ExitCode.NETWORK_FAILURE
    machine_code = "NETWORK_FAILURE"

    def __init__(
        self,
        message: str,
        hint: str | None = None,
        *,
        details: dict | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        # Transport failures can still carry safe, machine-readable context
        # (for example stream phase and the configured idle timeout). Keep it
        # separate from the user-facing message so clients do not parse prose.
        self.details = dict(details or {})

    @property
    def retryable(self) -> bool:
        return True


class ProviderModelError(RinariError):
    exit_code = ExitCode.PROVIDER_MODEL_FAILURE
    machine_code = "PROVIDER_MODEL_FAILURE"

    def __init__(
        self, message: str, hint: str | None = None, *, details: dict | None = None
    ) -> None:
        super().__init__(message, hint=hint)
        self.details = dict(details or {})


class ToolError(RinariError):
    exit_code = ExitCode.TOOL_FAILURE
    machine_code = "TOOL_FAILURE"


class CancelledError(RinariError):
    exit_code = ExitCode.CANCELLED
    machine_code = "CANCELLED"


class PartialCompletionError(RinariError):
    exit_code = ExitCode.PARTIAL_COMPLETION
    machine_code = "PARTIAL_COMPLETION"


class BlockedError(RinariError):
    exit_code = ExitCode.BLOCKED
    machine_code = "BLOCKED"


class CredentialStoreUnavailableError(RinariError):
    """The requested credential backend cannot operate on this system."""

    exit_code = ExitCode.CONFIGURATION_ERROR
    machine_code = "CREDENTIAL_STORE_UNAVAILABLE"


class HomeIdUnavailableError(RinariError):
    """Another process is publishing this home's identifier and the wait ended.

    Nunca se devuelve un identificador alternativo para el mismo home: dos
    procesos con ids distintos escribirían credenciales en namespaces que el
    otro no puede leer.
    """

    exit_code = ExitCode.CONFLICT
    machine_code = "HOME_ID_UNAVAILABLE"

    @property
    def retryable(self) -> bool:
        return True


class LockTimeoutError(RinariError):
    """Another process is mutating the same shared resource (for example the
    OS credential vault) and the lock could not be taken in time."""

    exit_code = ExitCode.CONFLICT
    machine_code = "CREDENTIAL_STORE_BUSY"

    @property
    def retryable(self) -> bool:
        return True


class CredentialWriteError(RinariError):
    """The OS credential store refused a write (for example a full vault).

    Raised before any previous value is removed, so a failed rotation keeps
    the stored secret usable (see the CredWrite error 8 report).
    """

    exit_code = ExitCode.GENERIC_FAILURE
    machine_code = "CREDENTIAL_STORE_WRITE_FAILED"

    def __init__(
        self, message: str, hint: str | None = None, *, details: dict | None = None
    ) -> None:
        super().__init__(message, hint=hint)
        self.details = dict(details or {})
