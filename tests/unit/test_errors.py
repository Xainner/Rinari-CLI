import pytest

from rinari.shared.errors import (
    ApprovalDeniedError,
    AuthenticationRequiredError,
    BlockedError,
    CancelledError,
    ConfigurationError,
    ConflictError,
    ExitCode,
    InvalidUsageError,
    NetworkError,
    NotFoundError,
    PartialCompletionError,
    PermissionDeniedError,
    ProviderModelError,
    RinariError,
    ToolError,
    ValidationFailureError,
)

CASES = [
    (RinariError, ExitCode.GENERIC_FAILURE),
    (InvalidUsageError, ExitCode.INVALID_USAGE),
    (ConfigurationError, ExitCode.CONFIGURATION_ERROR),
    (AuthenticationRequiredError, ExitCode.AUTHENTICATION_REQUIRED),
    (PermissionDeniedError, ExitCode.PERMISSION_DENIED),
    (ApprovalDeniedError, ExitCode.APPROVAL_DENIED),
    (NotFoundError, ExitCode.NOT_FOUND),
    (ConflictError, ExitCode.CONFLICT),
    (ValidationFailureError, ExitCode.VALIDATION_FAILED),
    (NetworkError, ExitCode.NETWORK_FAILURE),
    (ProviderModelError, ExitCode.PROVIDER_MODEL_FAILURE),
    (ToolError, ExitCode.TOOL_FAILURE),
    (CancelledError, ExitCode.CANCELLED),
    (PartialCompletionError, ExitCode.PARTIAL_COMPLETION),
    (BlockedError, ExitCode.BLOCKED),
]


@pytest.mark.parametrize(("cls", "code"), CASES)
def test_exit_code_mapping(cls, code):
    err = cls("boom")
    assert err.code is code
    assert err.exit_code == int(code)


def test_error_carries_message_and_hint():
    err = NotFoundError("no provider 'opus'", hint="Run `rinari providers list`.")
    assert err.message == "no provider 'opus'"
    assert err.hint == "Run `rinari providers list`."


def test_exit_codes_are_stable_contract():
    assert ExitCode.SUCCESS == 0
    assert ExitCode.INVALID_USAGE == 2
    assert ExitCode.NOT_FOUND == 7
    assert ExitCode.BLOCKED == 15
