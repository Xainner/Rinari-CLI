"""Provider type catalog and adapter factory.

`PROVIDER_TYPES` is the set of provider families `providers add` accepts
in phase 1; adding a family means adding an adapter and a catalog entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from rinari.providers.adapters.anthropic import AnthropicAdapter
from rinari.providers.adapters.base import ProviderAdapter
from rinari.providers.adapters.openai_compatible import OpenAICompatibleAdapter
from rinari.shared.errors import InvalidUsageError
from rinari.storage.records import ProviderRecord

KNOWN_PROTOCOLS = ("openai-compatible", "anthropic-compatible")


@dataclass(frozen=True, slots=True)
class ProviderTypeSpec:
    name: str
    adapter: type[ProviderAdapter]
    auth_methods: tuple[str, ...]
    default_base_url: str | None = None


PROVIDER_TYPES: dict[str, ProviderTypeSpec] = {
    "openai": ProviderTypeSpec(
        name="openai",
        adapter=OpenAICompatibleAdapter,
        auth_methods=("api-key", "none"),
        default_base_url="https://api.openai.com/v1",
    ),
    "anthropic": ProviderTypeSpec(
        name="anthropic",
        adapter=AnthropicAdapter,
        auth_methods=("api-key",),
    ),
    "custom": ProviderTypeSpec(
        name="custom",
        adapter=OpenAICompatibleAdapter,
        auth_methods=("api-key", "none"),
    ),
}


def adapter_for(record: ProviderRecord, client: httpx.Client | None = None) -> ProviderAdapter:
    settings: dict[str, Any] = record.settings or {}
    protocol = settings.get("protocol", "openai-compatible")
    if protocol == "anthropic-compatible" or record.type == "anthropic":
        return AnthropicAdapter(client=client)
    if protocol not in KNOWN_PROTOCOLS:
        raise InvalidUsageError(
            f"Unknown adapter protocol for provider {record.alias!r}: {protocol}",
            hint=f"Supported protocols: {', '.join(KNOWN_PROTOCOLS)}.",
        )
    default_base_url = None
    if record.type == "openai":
        default_base_url = "https://api.openai.com/v1"
    elif record.type in PROVIDER_TYPES:
        default_base_url = PROVIDER_TYPES[record.type].default_base_url
    return OpenAICompatibleAdapter(
        default_base_url=default_base_url or settings.get("base_url"), client=client
    )


def validate_provider_type(
    provider_type: str, auth_method: str, protocol: str | None = None
) -> ProviderTypeSpec:
    if provider_type not in PROVIDER_TYPES:
        raise InvalidUsageError(
            f"Unknown provider type: {provider_type}",
            hint=f"Supported types: {', '.join(PROVIDER_TYPES)}.",
        )
    spec = PROVIDER_TYPES[provider_type]
    if protocol is not None and protocol not in KNOWN_PROTOCOLS:
        raise InvalidUsageError(
            f"Unknown protocol: {protocol}",
            hint=f"Supported protocols: {', '.join(KNOWN_PROTOCOLS)}.",
        )
    if auth_method not in spec.auth_methods:
        supported = " ".join(
            f"--{'api-key' if m == 'api-key' else 'no-auth'}" for m in spec.auth_methods
        )
        raise InvalidUsageError(
            f"Auth method {auth_method!r} is not supported by provider type {provider_type!r}",
            hint=f"Supported: {', '.join(spec.auth_methods)} ({supported}).",
        )
    return spec
