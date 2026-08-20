"""Shared HTTP helpers for provider adapters.

Transport/timeout failures become NetworkError (exit 10). Status-code
interpretation (auth vs. provider errors) belongs to each adapter.
"""

from __future__ import annotations

import httpx

from rinari.shared.errors import NetworkError, ProviderModelError

# Model generation can run for minutes; the client default (10s) only fits
# discovery/health probes.
MODEL_CALL_TIMEOUT = 600.0


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


def auth_failure(response: httpx.Response, url: str) -> ProviderModelError:
    return ProviderModelError(
        f"Authentication failed (HTTP {response.status_code}) for {url}",
        hint="Check the provider credential: `rinari providers auth <alias>`.",
    )


def provider_error(response: httpx.Response, url: str) -> ProviderModelError:
    return ProviderModelError(f"Provider returned HTTP {response.status_code} for {url}")


def decode_json(response: httpx.Response, url: str):
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderModelError(f"Provider returned invalid JSON for {url}") from exc
