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
    # The account cannot keep spending: plan quota used up, no credits or a
    # spend cap. Waiting a few seconds does not fix it.
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"


# What the provider's own error fields (code, type, status) say about a
# limit. These are protocol values the providers document, never words read
# out of the human-readable message. Anything else keeps HTTP semantics.
_QUOTA_CODES = frozenset(
    {
        "insufficient_quota",
        "billing_hard_limit_reached",
        "billing_not_active",
        "insufficient_balance",
        "insufficient_credits",
        "credit_balance_too_low",
        "quota_exceeded",
        "usage_limit_reached",
        "spend_limit_reached",
        "payment_required",
    }
)
_RATE_CODES = frozenset(
    {"rate_limit_exceeded", "rate_limit_error", "rate_limited", "too_many_requests"}
)
_CONTEXT_CODES = frozenset(
    {"context_length_exceeded", "context_window_exceeded", "max_context_length_exceeded"}
)
_OVERLOAD_CODES = frozenset(
    {"server_error", "overloaded_error", "api_error", "service_unavailable", "timeout"}
)


def _limit_kind(*fields: str | None) -> str | None:
    """`quota`, `rate` or None, from the provider's structured error fields."""
    values = {str(f).lower() for f in fields if f}
    if values & _QUOTA_CODES:
        return "quota"
    if values & _RATE_CODES:
        return "rate"
    return None


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
        if retry_after is not None:
            # The wait the provider asked for this request; not a quota reset.
            details["retry_after_s"] = retry_after
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
    """Seconds from `Retry-After`, given as a number or as an HTTP date."""
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    return max(0.0, round(when.timestamp() - time.time(), 3))


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
    if isinstance(error, str):
        return error[:300], None, None
    # `{"error": {...}}` is the common envelope; some providers put the same
    # fields at the top level. Google calls its type `status`.
    source = error if isinstance(error, dict) else payload
    raw_message = source.get("message")
    message = str(raw_message)[:300] if isinstance(raw_message, str) else ""
    raw_type = source.get("type") or source.get("status")
    error_type = str(raw_type)[:100] if isinstance(raw_type, str) else None
    raw_code = source.get("code")
    error_code = (
        str(raw_code)[:100]
        if isinstance(raw_code, (str, int)) and not isinstance(raw_code, bool)
        else None
    )
    return message, error_type, error_code


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
    """Map an HTTP failure to the taxonomy, preserving legacy messages.

    The provider's own code/type decides before the HTTP status: a quota
    code on a 403 is still a quota. Every result carries the fields the UI
    needs to explain it (status, provider code/type, retry wait).
    """
    error = _classify_http_error(response, url, provider=provider, model=model)
    _, provider_error_type, provider_error_code = _safe_error_fields(_error_payload(response))
    if provider_error_type:
        error.details.setdefault("provider_error_type", provider_error_type)
    if provider_error_code:
        error.details.setdefault("provider_response_code", provider_error_code)
    error.details.setdefault("http_status", response.status_code)
    return error


def _classify_http_error(
    response: httpx.Response,
    url: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> ProviderError:
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
    limit = _limit_kind(provider_error_code, provider_error_type)
    if limit == "quota" or status == 402:
        result = ProviderError(
            message,
            code=ProviderErrorCode.QUOTA_EXHAUSTED,
            retryable=False,
            hint="The provider account has no quota or credits left for this request.",
            **common,
        )
        result.details["limit_kind"] = "quota"
        return result
    if status in (401, 403):
        return ProviderError(
            message,
            code=ProviderErrorCode.AUTH,
            retryable=False,
            hint=(
                "Check the provider credential: `rinari providers auth <alias>`."
                if status == 401
                else "The provider refused this request: check the credential and that "
                "it may use this model."
            ),
            **common,
        )
    if status == 404:
        return ProviderError(message, code=ProviderErrorCode.MODEL_NOT_FOUND, **common)
    if status == 408:
        return ProviderError(message, code=ProviderErrorCode.TIMEOUT, retryable=True, **common)
    # Only an explicit modality rejection authorizes visual fallback. A generic
    # 400, bad image encoding, quota or authentication failure does not.
    if status in (400, 422):
        code = (provider_error_code or "").lower()
        if code in _CONTEXT_CODES:
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
        schema_signal = code in {
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
    if status == 429 or limit == "rate":
        result = ProviderError(
            message,
            code=ProviderErrorCode.RATE_LIMIT,
            retryable=True,
            retry_after=_retry_after(response),
            **common,
        )
        # Without a code the provider did not say whether it is a rate or a
        # quota: the UI says so instead of guessing.
        result.details["limit_kind"] = limit or "unknown"
        return result
    if status >= 500 or status in (408, 409, 425, 502, 503, 504):
        return ProviderError(message, code=ProviderErrorCode.SERVER_ERROR, retryable=True, **common)
    return ProviderError(message, code=ProviderErrorCode.SERVER_ERROR, **common)


def classify_stream_error(raw: Any, *, model: str | None = None) -> ProviderError:
    """An error the provider sent inside an SSE stream, on the same taxonomy.

    The provider's message becomes the error's message (it used to stay
    nested under a generic "stream failure"), and its code/type pick the
    category like an HTTP error would. `details.provider_error` keeps the
    original object for diagnostics.
    """
    if isinstance(raw, dict):
        fields = raw
    elif isinstance(raw, str):
        fields = {"message": raw}
    else:
        fields = {}
    message, error_type, error_code = _safe_error_fields({"error": fields} if fields else None)
    limit = _limit_kind(error_code, error_type)
    values = {str(v).lower() for v in (error_code, error_type) if v}
    if limit == "quota":
        code, retryable = ProviderErrorCode.QUOTA_EXHAUSTED, False
    elif limit == "rate":
        code, retryable = ProviderErrorCode.RATE_LIMIT, True
    elif values & _CONTEXT_CODES:
        code, retryable = ProviderErrorCode.CONTEXT_OVERFLOW, False
    elif values & _OVERLOAD_CODES:
        code, retryable = ProviderErrorCode.SERVER_ERROR, True
    else:
        code, retryable = ProviderErrorCode.STREAM_INTERRUPTED, False
    if message:
        text = f"Provider reported a stream failure: {message}"
    else:
        text = "Provider reported stream failure"
    result = ProviderError(text, code=code, retryable=retryable, model=model)
    result.details["provider_error"] = raw
    if error_type:
        result.details["provider_error_type"] = error_type
    if error_code:
        result.details["provider_response_code"] = error_code
    if limit:
        result.details["limit_kind"] = limit
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
