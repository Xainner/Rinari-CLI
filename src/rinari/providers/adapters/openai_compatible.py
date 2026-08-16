"""OpenAI-compatible adapter: OpenAI, Ollama, LM Studio, custom endpoints."""

from __future__ import annotations

from typing import Any

from rinari.providers.adapters.base import AuthStatus, DiscoveredModel, ProviderAdapter
from rinari.providers.adapters.http import (
    auth_failure,
    decode_json,
    provider_error,
    send_request,
)


class OpenAICompatibleAdapter(ProviderAdapter):
    type = "openai-compatible"

    def __init__(
        self,
        default_base_url: str | None = None,
        *,
        client=None,
    ) -> None:
        super().__init__(client=client)
        self.default_base_url = default_base_url

    def auth_methods(self) -> tuple[str, ...]:
        return ("api-key", "none")

    def base_url(self, endpoint: str | None, settings: dict[str, Any] | None = None) -> str:
        if not (endpoint or self.default_base_url):
            from rinari.shared.errors import ConfigurationError

            raise ConfigurationError(
                "No endpoint configured",
                hint="Custom OpenAI-compatible providers need an endpoint, e.g. "
                "`--endpoint http://localhost:11434/v1`.",
            )
        return (endpoint or self.default_base_url).rstrip("/")

    def _headers(self, secret: str | None) -> dict[str, str]:
        if secret:
            return {"Authorization": f"Bearer {secret}"}
        return {}

    def validate_credential(self, secret: str | None, endpoint: str | None = None) -> AuthStatus:
        url = f"{self.base_url(endpoint, None)}/models"
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
        url = f"{self.base_url(endpoint, None)}/models"
        response = send_request(self.client(), "GET", url, headers=self._headers(secret))
        if response.status_code in (401, 403):
            raise auth_failure(response, url)
        if response.status_code >= 400:
            raise provider_error(response, url)
        data = decode_json(response, url) or {}
        entries = data.get("data", data if isinstance(data, list) else [])
        models: list[DiscoveredModel] = []
        for entry in entries:
            model_id = entry.get("id") if isinstance(entry, dict) else str(entry)
            if not model_id:
                continue
            models.append(
                DiscoveredModel(provider_model_id=str(model_id), availability="available")
            )
        return models
