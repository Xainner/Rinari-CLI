"""Shared HTTP helpers for provider adapters.

Transport/timeout failures become NetworkError (exit 10). Status-code
interpretation (auth vs. provider errors) belongs to each adapter.
"""

from __future__ import annotations

import re

import httpx

from rinari.shared.errors import NetworkError, ProviderModelError

# Model generation can run for minutes; the client default (10s) only fits
# discovery/health probes.
MODEL_CALL_TIMEOUT = 600.0

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
        raise NetworkError(f"Timed out contacting {url}") from exc
    except httpx.TransportError as exc:
        raise NetworkError(f"Cannot reach {url}: {exc.__class__.__name__}") from exc


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
