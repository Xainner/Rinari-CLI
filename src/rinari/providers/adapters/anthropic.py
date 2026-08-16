"""Anthropic adapter: https://api.anthropic.com (x-api-key + anthropic-version)."""

from __future__ import annotations

from typing import Any

from rinari.providers.adapters.base import AuthStatus, DiscoveredModel, ProviderAdapter
from rinari.providers.adapters.http import (
    auth_failure,
    decode_json,
    provider_error,
    send_request,
)
from rinari.shared.errors import ProviderModelError

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"


class AnthropicAdapter(ProviderAdapter):
    type = "anthropic"

    def __init__(self, *, client=None) -> None:
        super().__init__(client=client)

    def auth_methods(self) -> tuple[str, ...]:
        return ("api-key",)

    def base_url(self, endpoint: str | None, settings: dict[str, Any] | None = None) -> str:
        return (endpoint or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self, secret: str | None) -> dict[str, str]:
        headers = {"anthropic-version": API_VERSION}
        if secret:
            headers["x-api-key"] = secret
        return headers

    def validate_credential(self, secret: str | None, endpoint: str | None = None) -> AuthStatus:
        url = f"{self.base_url(endpoint, None)}/v1/models?limit=1"
        response = send_request(self.client(), "GET", url, headers=self._headers(secret))
        if response.status_code in (401, 403):
            return AuthStatus(
                connected=False, detail=f"authentication failed (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            return AuthStatus(
                connected=False, detail=f"endpoint error (HTTP {response.status_code})"
            )
        return AuthStatus(connected=True, detail="ok")

    def list_models(self, secret: str | None, endpoint: str | None = None) -> list[DiscoveredModel]:
        url = f"{self.base_url(endpoint, None)}/v1/models?limit=100"
        response = send_request(self.client(), "GET", url, headers=self._headers(secret))
        if response.status_code in (401, 403):
            raise auth_failure(response, url)
        if response.status_code >= 400:
            raise provider_error(response, url)
        data = decode_json(response, url)
        if not isinstance(data, dict):
            raise ProviderModelError(f"Unexpected model list payload for {url}")
        return [
            DiscoveredModel(provider_model_id=str(item["id"]), availability="available")
            for item in data.get("data", [])
            if isinstance(item, dict) and item.get("id")
        ]
