"""Provider error taxonomy + model-call retry (Etapa D / review §6.3-6.4).

All provider failures normalize to ProviderError (a ProviderModelError, so
existing handlers keep working) carrying a machine code, retryability, an
optional retry_after hint, request id, and provider/model labels. The model
runtime can then decide: retry same / fallback model / compact context /
reduce tools / ask auth / stop.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any, TypeVar

import httpx

from rinari.shared.errors import ProviderModelError


class ProviderErrorCode(StrEnum):
    AUTH = "AUTH"
    RATE_LIMIT = "RATE_LIMIT"
    CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"
    INVALID_TOOL_SCHEMA = "INVALID_TOOL_SCHEMA"
    INVALID_TOOL_ARGUMENTS = "INVALID_TOOL_ARGUMENTS"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    SAFETY_BLOCK = "SAFETY_BLOCK"
    SERVER_ERROR = "SERVER_ERROR"
    STREAM_INTERRUPTED = "STREAM_INTERRUPTED"
    TIMEOUT = "TIMEOUT"


class ProviderError(ProviderModelError):
    """Normalized provider failure with routing metadata."""

    def __init__(
        self,
        message: str,
        *,
        code: ProviderErrorCode = ProviderErrorCode.SERVER_ERROR,
        retryable: bool = False,
        retry_after: float | None = None,
        request_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        # NOTE: RinariError.code stays the ExitCode property; the taxonomy
        # code lives here so exit-code mapping never breaks.
        self.error_code = code
        self._retryable = retryable
        self.retry_after = retry_after
        self.request_id = request_id
        self.provider = provider
        self.model = model

    @property
    def retryable(self) -> bool:
        return self._retryable


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _request_id(response: httpx.Response) -> str | None:
    for header in ("x-request-id", "x-requestid", "request-id"):
        value = response.headers.get(header)
        if value:
            return value
    return None


def classify_http_error(
    response: httpx.Response,
    url: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> ProviderError:
    """Map an HTTP failure to the taxonomy, preserving legacy messages."""
    status = response.status_code
    try:
        payload = response.json()
    except Exception:
        payload = None
    detail = ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            detail = f": {str(error['message'])[:300]}"
        elif isinstance(error, str) and error:
            detail = f": {error[:300]}"
    message = f"Provider returned HTTP {status} for {url}{detail}"
    common: dict[str, Any] = {
        "request_id": _request_id(response),
        "provider": provider,
        "model": model,
    }
    if status in (401, 403):
        return ProviderError(
            message,
            code=ProviderErrorCode.AUTH,
            retryable=False,
            hint="Check the provider credential: `rinari providers auth <alias>`.",
            **common,
        )
    if status == 404:
        return ProviderError(message, code=ProviderErrorCode.MODEL_NOT_FOUND, **common)
    if status == 408:
        return ProviderError(message, code=ProviderErrorCode.TIMEOUT, retryable=True, **common)
    if status == 422:
        return ProviderError(message, code=ProviderErrorCode.INVALID_TOOL_ARGUMENTS, **common)
    if status == 429:
        return ProviderError(
            message,
            code=ProviderErrorCode.RATE_LIMIT,
            retryable=True,
            retry_after=_retry_after(response),
            **common,
        )
    if status >= 500 or status in (408, 409, 425, 502, 503, 504):
        return ProviderError(message, code=ProviderErrorCode.SERVER_ERROR, retryable=True, **common)
    return ProviderError(message, code=ProviderErrorCode.SERVER_ERROR, **common)


T = TypeVar("T")

# §6.4: retry with backoff for 429 / 5xx / reset / temporary timeout —
# only when no ambiguous final response was produced (clean failure).
RETRYABLE_MODEL_CODES = frozenset(
    {
        ProviderErrorCode.RATE_LIMIT,
        ProviderErrorCode.SERVER_ERROR,
        ProviderErrorCode.TIMEOUT,
        ProviderErrorCode.MODEL_UNAVAILABLE,
    }
)


def invoke_with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay_s: float = 1.0,
    max_delay_s: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run a model call, retrying transient provider failures with backoff."""
    delay = base_delay_s
    last: ProviderError | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except ProviderError as exc:
            last = exc
            retryable = exc.retryable and exc.error_code in RETRYABLE_MODEL_CODES
            if not retryable or attempt >= max(1, attempts):
                raise
            wait = exc.retry_after if exc.retry_after is not None else delay
            sleep(min(wait, max_delay_s))
            delay = min(delay * 2.0, max_delay_s)
    assert last is not None  # pragma: no cover - loop always runs once
    raise last
