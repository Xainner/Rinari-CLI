"""Shared HTTP helpers for provider adapters.

Transport/timeout failures become NetworkError (exit 10). Status-code
interpretation (auth vs. provider errors) belongs to each adapter.
"""

from __future__ import annotations

import re
import time

import httpx

from rinari.shared.errors import NetworkError, ProviderModelError

# Model generation can run for minutes; the client default (10s) only fits
# discovery/health probes.
MODEL_CALL_TIMEOUT = 600.0
# Streaming calls need a much shorter inactivity bound. A healthy long-running
# response may keep streaming for minutes, but waiting ten minutes for the first
# byte makes a dead provider look like a permanently thinking model.
MODEL_STREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=10.0)
DEFAULT_MODEL_STREAM_READ_TIMEOUT_S = 30.0
MIN_MODEL_STREAM_READ_TIMEOUT_S = 1.0
MAX_MODEL_STREAM_READ_TIMEOUT_S = 600.0

OPENCODE_SESSION_HEADER = "x-opencode-session"

_TOOL_NAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def _is_opencode_host(url: str | None) -> bool:
    if not url:
        return False
    host = httpx.URL(url).host or ""
    return host == "opencode.ai" or host.endswith(".opencode.ai")


def session_affinity_headers(url: str | None, session_id: str | None) -> dict[str, str]:
    """Vendor session-affinity headers for model calls.

    OpenCode requires a stable per-conversation id on its own endpoints
    (enforced 2026-09-06); requests without it may error. Only sent to
    the vendor's own hosts and only when the caller propagated a session
    id. The value is Rinari's opaque session id (no secret material).
    """
    if not session_id or not _is_opencode_host(url):
        return {}
    return {OPENCODE_SESSION_HEADER: session_id}


def sanitize_tool_name(name: str) -> str:
    """Rewrite one tool name to the strict function-name pattern.

    Strict OpenAI-compatible vendors (e.g. OpenCode Go) reject names
    outside ^[a-zA-Z0-9_-]+$; Rinari's dotted names (fs.read) fall in
    that bucket. Fellow of needs_tool_aliasing: only applied where the
    vendor requires it, and mapped back on the way in.
    """
    return _TOOL_NAME_UNSAFE.sub("_", name)


def is_opencode_endpoint(url: str | None) -> bool:
    """Whether this URL belongs to the vendor's own API hosts."""
    return _is_opencode_host(url)


def needs_tool_aliasing(endpoint: str | None) -> bool:
    """Whether tool names must be sanitized for this endpoint."""
    return _is_opencode_host(endpoint)


def send_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    timeout: float | None = None,
) -> httpx.Response:
    try:
        return client.request(method, url, headers=headers, json=json_body, timeout=timeout)
    except httpx.TimeoutException as exc:
        timeout_s = timeout if isinstance(timeout, (int, float)) else None
        raise NetworkError(
            f"Timed out contacting {url}",
            details={
                "kind": "TIMEOUT",
                "phase": "request",
                "timeout_s": timeout_s,
            },
        ) from exc
    except httpx.TransportError as exc:
        raise NetworkError(f"Cannot reach {url}: {exc.__class__.__name__}") from exc


def model_stream_timeout(read_timeout_s: float | None = None) -> httpx.Timeout:
    """Build the bounded streaming timeout used by model adapters."""
    read = DEFAULT_MODEL_STREAM_READ_TIMEOUT_S if read_timeout_s is None else read_timeout_s
    return httpx.Timeout(connect=15.0, read=read, write=30.0, pool=10.0)


def stream_timeout_error(
    url: str,
    model: str,
    exc: httpx.TimeoutException,
    *,
    timeout_s: float,
    headers_received: bool,
    saw_payload: bool,
    stream_started_at: float,
    last_activity_at: float,
) -> NetworkError:
    """Normalize a stream timeout without losing where the wait occurred."""
    if isinstance(exc, httpx.ConnectTimeout):
        phase = "connect"
        effective_timeout_s = 15.0
    elif isinstance(exc, httpx.PoolTimeout):
        phase = "pool"
        effective_timeout_s = 10.0
    elif isinstance(exc, httpx.WriteTimeout):
        phase = "write"
        effective_timeout_s = 30.0
    elif isinstance(exc, httpx.ReadTimeout) and not headers_received:
        phase = "response_headers"
        effective_timeout_s = timeout_s
    elif isinstance(exc, httpx.ReadTimeout) and not saw_payload:
        phase = "first_byte"
        effective_timeout_s = timeout_s
    elif isinstance(exc, httpx.ReadTimeout):
        phase = "between_chunks"
        effective_timeout_s = timeout_s
    else:
        phase = "read"
        effective_timeout_s = timeout_s
    now = time.monotonic()
    last_payload_at_s = (
        round(last_activity_at - stream_started_at, 3) if saw_payload else None
    )
    return NetworkError(
        f"Timed out streaming from {url}",
        details={
            "kind": "TIMEOUT",
            "phase": phase,
            "timeout_s": effective_timeout_s,
            # These measure complete provider payload lines, not raw socket
            # bytes, because httpx exposes the stream to us at line boundaries.
            "last_payload_at_s": last_payload_at_s,
            "payload_idle_s": round(now - last_activity_at, 3),
            "headers_received": headers_received,
            "saw_payload": saw_payload,
            "partial": saw_payload,
            "model": model,
        },
    )


def provider_error_detail(response: httpx.Response, url: str) -> str:
    """Best-effort human detail from an error body (no secret leakage)."""
    # A streaming response body is not loaded until read(); without this the
    # error path itself crashes with httpx.ResponseNotRead, masking the real
    # provider error (openai/anthropic invoke_stream pass unread responses).
    try:
        if not response.is_stream_consumed:
            response.read()
    except Exception:
        return f"Provider returned HTTP {response.status_code} for {url}"
    try:
        data = response.json()
    except ValueError:
        return f"Provider returned HTTP {response.status_code} for {url}"
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return f"Provider returned HTTP {response.status_code}: {message[:300]}"
    return f"Provider returned HTTP {response.status_code} for {url}"


def auth_failure(
    response: httpx.Response,
    url: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> ProviderModelError:
    from rinari.providers.errors import ProviderError, ProviderErrorCode

    return ProviderError(
        f"Authentication failed (HTTP {response.status_code}) for {url}",
        code=ProviderErrorCode.AUTH,
        retryable=False,
        provider=provider,
        model=model,
        hint="Check the provider credential: `rinari providers auth <alias>`.",
    )


def provider_error(
    response: httpx.Response,
    url: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> ProviderModelError:
    from rinari.providers.errors import classify_http_error

    return classify_http_error(response, url, provider=provider, model=model)


def decode_json(response: httpx.Response, url: str):
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderModelError(f"Provider returned invalid JSON for {url}") from exc
