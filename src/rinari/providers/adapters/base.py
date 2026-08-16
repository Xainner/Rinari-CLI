"""Provider adapter contract (docs/commands.md section 73).

Adapters normalize a provider family into a common surface: auth
capabilities, credential validation, model discovery, and health checks.
Model invocation joins this contract in phase 2 (Model Runtime).

Adapters accept an injectable `httpx.Client` so tests can use
`httpx.MockTransport`; no adapter opens real network connections in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class AuthStatus:
    connected: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    provider_model_id: str
    capabilities: dict[str, Any] | None = None
    availability: str = "available"


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    connected: bool
    detail: str = ""
    models_discovered: int = 0
    models: list[DiscoveredModel] = field(default_factory=list)


class ProviderAdapter:
    """One adapter per provider family. Subclasses override capabilities."""

    type: str = "abstract"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=10.0)
        return self._client

    def auth_methods(self) -> tuple[str, ...]:
        raise NotImplementedError

    def login(self) -> AuthStatus:
        """Interactive login flow (OAuth device flow and friends).

        Adapters without a built-in interactive flow leave this as the
        default; the Provider Service translates that into a guidance
        error pointing at the API-key flows.
        """
        raise NotImplementedError

    def base_url(self, endpoint: str | None, settings: dict[str, Any] | None = None) -> str:
        return endpoint or ""

    def validate_credential(self, secret: str | None, endpoint: str | None = None) -> AuthStatus:
        raise NotImplementedError

    def list_models(self, secret: str | None, endpoint: str | None = None) -> list[DiscoveredModel]:
        raise NotImplementedError

    def health(self, secret: str | None, endpoint: str | None = None) -> ProviderHealth:
        auth = self.validate_credential(secret, endpoint)
        if not auth.connected:
            return ProviderHealth(connected=False, detail=auth.detail)
        models = self.list_models(secret, endpoint)
        return ProviderHealth(
            connected=True,
            detail=f"{len(models)} model(s) discovered",
            models_discovered=len(models),
            models=models,
        )
