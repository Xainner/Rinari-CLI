"""Shared HTTP helpers for provider adapters.

Transport/timeout failures become NetworkError (exit 10). Status-code
interpretation (auth vs. provider errors) belongs to each adapter.
"""

from __future__ import annotations

import httpx

from rinari.shared.errors import NetworkError, ProviderModelError


def send_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    try:
        return client.request(method, url, headers=headers)
    except httpx.TimeoutException as exc:
        raise NetworkError(f"Timed out contacting {url}") from exc
    except httpx.TransportError as exc:
        raise NetworkError(f"Cannot reach {url}: {exc.__class__.__name__}") from exc


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
