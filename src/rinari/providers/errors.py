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
    VISION_UNSUPPORTED = "VISION_UNSUPPORTED"


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
        details: dict[str, Any] = {"provider_error_code": code.value}
        if request_id:
            details["request_id"] = request_id
        if provider:
            details["provider"] = provider
        if model:
            details["model"] = model
        super().__init__(message, hint=hint, details=details)
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


def _request_id(response: httpx.Response, payload: Any = None) -> str | None:
    for header in ("x-request-id", "x-requestid", "request-id"):
        value = response.headers.get(header)
        if value:
            return value
    if isinstance(payload, dict):
        value = payload.get("request_id")
        if isinstance(value, str) and value:
            return value
    return None


def _error_payload(response: httpx.Response) -> dict[str, Any] | None:
    """Consume and decode a provider error response without leaking raw data.

    ``httpx.Client.stream`` leaves the body unread.  Error classification is
    the common boundary for streaming and non-streaming calls, so it must read
    the body before asking httpx to decode JSON.  The raw body is deliberately
    never returned or attached to the exception.
    """
    try:
        if not response.is_stream_consumed:
            response.read()
        payload = response.json()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _safe_error_fields(payload: dict[str, Any] | None) -> tuple[str, str | None, str | None]:
    """Return bounded message/type/code fields from common provider envelopes."""
    if not payload:
        return "", None, None
    error = payload.get("error")
    if isinstance(error, dict):
        raw_message = error.get("message")
        message = str(raw_message)[:300] if isinstance(raw_message, str) else ""
        raw_type = error.get("type")
        error_type = str(raw_type)[:100] if isinstance(raw_type, str) else None
        raw_code = error.get("code")
        error_code = str(raw_code)[:100] if isinstance(raw_code, (str, int)) else None
        return message, error_type, error_code
    if isinstance(error, str):
        return error[:300], None, None
    return "", None, None


def provider_error_message(response: httpx.Response, url: str) -> str:
    """Build the safe, bounded message shared by all HTTP adapter paths."""
    payload = _error_payload(response)
    detail, _, _ = _safe_error_fields(payload)
    suffix = f": {detail}" if detail else ""
    return f"Provider returned HTTP {response.status_code} for {url}{suffix}"


def classify_http_error(
    response: httpx.Response,
    url: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> ProviderError:
    """Map an HTTP failure to the taxonomy, preserving legacy messages."""
    status = response.status_code
    payload = _error_payload(response)
    error_message, provider_error_type, provider_error_code = _safe_error_fields(payload)
    detail = f": {error_message}" if error_message else ""
    message = f"Provider returned HTTP {status} for {url}{detail}"
    request_id = _request_id(response, payload)
    common: dict[str, Any] = {
        "request_id": request_id,
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
    # Only an explicit modality rejection authorizes visual fallback. A generic
    # 400, bad image encoding, quota or authentication failure does not.
    if status in (400, 422):
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        code = str(error.get("code", "")) if isinstance(error, dict) else ""
        if code in {
            "context_length_exceeded",
            "context_window_exceeded",
            "max_context_length_exceeded",
        }:
            return ProviderError(message, code=ProviderErrorCode.CONTEXT_OVERFLOW, **common)
        text = detail.lower()
        explicit = code in {
            "vision_not_supported",
            "unsupported_image_input",
            "image_input_not_supported",
        }
        explicit = explicit or any(
            phrase in text
            for phrase in (
                "does not support image",
                "doesn't support image",
                "image inputs are not supported",
                "image input is not supported",
                "does not support vision",
                "only supports text input",
                "image_url is only supported by certain models",
            )
        )
        if explicit:
            return ProviderError(message, code=ProviderErrorCode.VISION_UNSUPPORTED, **common)
        schema_signal = (provider_error_code or "").lower() in {
            "invalid_tool_schema",
            "invalid_function_parameters",
        } or any(
            marker in error_message.lower()
            for marker in ("input_schema", "tool schema", "function schema")
        )
        if schema_signal:
            return ProviderError(message, code=ProviderErrorCode.INVALID_TOOL_SCHEMA, **common)
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
    result = ProviderError(message, code=ProviderErrorCode.SERVER_ERROR, **common)
    if provider_error_type:
        result.details["provider_error_type"] = provider_error_type
    if provider_error_code:
        result.details["provider_response_code"] = provider_error_code
    result.details["http_status"] = status
    return result


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
