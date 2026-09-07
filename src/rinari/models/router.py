"""Model router (harness.md section 82).

Phase-2 implementation: manually selected provider/model only (no
auto-routing). The router takes already-resolved provider and model
references, resolves the credential, and dispatches the normalized
`ModelRequest` to the matching adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

import httpx

from rinari.providers.adapters.http import needs_tool_aliasing, sanitize_tool_name
from rinari.providers.registry import adapter_for
from rinari.shared.errors import InvalidUsageError

if TYPE_CHECKING:
    from rinari.application.model_service import ModelService
    from rinari.application.provider_service import ProviderService
    from rinari.models.types import ModelRequest, ModelResponse, ProviderCapabilities
    from rinari.storage.records import ProviderRecord


def _alias_request_tools(
    endpoint: str | None, request: ModelRequest
) -> tuple[ModelRequest, dict[str, str] | None]:
    """Rewrite tool names for vendors with strict function-name patterns.

    Returns the (possibly rewritten) request plus an alias->real map for
    the way back. Off-vendor or tool-less requests pass through untouched
    (map None), so every other provider sees byte-identical payloads.
    """
    if not request.tools or not needs_tool_aliasing(endpoint):
        return request, None
    real_by_alias: dict[str, str] = {}
    for tool in sorted(request.tools, key=lambda t: t.name):
        base = sanitize_tool_name(tool.name)
        alias, n = base, 2
        while alias in real_by_alias and real_by_alias[alias] != tool.name:
            alias = f"{base}__{n}"
            n += 1
        real_by_alias[alias] = tool.name
    alias_of = {real: alias for alias, real in real_by_alias.items()}
    return (
        replace(request, tools=tuple(replace(t, name=alias_of[t.name]) for t in request.tools)),
        real_by_alias,
    )


def _unalias_response(
    response: ModelResponse, real_by_alias: dict[str, str] | None
) -> ModelResponse:
    """Restore registry tool names on model-requested calls.

    Unknown names pass through; execution then fails with the usual
    not-found error instead of a silent mismatch.
    """
    if not real_by_alias or not response.tool_calls:
        return response
    return replace(
        response,
        tool_calls=tuple(
            replace(tc, name=real_by_alias.get(tc.name, tc.name)) for tc in response.tool_calls
        ),
    )


class ModelRouter:
    def __init__(
        self,
        providers: ProviderService,
        models: ModelService,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._providers = providers
        self._models = models
        self._client = http_client

    def adapter(self, provider: ProviderRecord):
        return adapter_for(provider, self._client)

    def capabilities(self, provider: ProviderRecord) -> ProviderCapabilities:
        return self.adapter(provider).capabilities()

    def _resolve_model(self, provider: ProviderRecord, model_id: str | None):
        if model_id is None:
            raise InvalidUsageError(
                "No model selected for this provider",
                hint="Add one first: `rinari models add --provider <alias> <model>`.",
            )
        model = self._models.resolve(model_id)
        if model.provider_id != provider.id:
            owner = self._providers.get(model.provider_id).alias
            raise InvalidUsageError(
                f"Model {model.alias!r} belongs to provider {owner!r}, not {provider.alias!r}",
                hint="Select the matching provider or a model of this provider.",
            )
        return model

    def invoke(
        self,
        provider: ProviderRecord,
        model_id: str | None,
        request: ModelRequest,
    ) -> ModelResponse:
        model = self._resolve_model(provider, model_id)
        request = replace(request, model=model.provider_model_id)
        request, real_by_alias = _alias_request_tools(provider.endpoint, request)
        return _unalias_response(self._adapter_invoke(provider, request), real_by_alias)

    def invoke_stream(
        self,
        provider: ProviderRecord,
        model_id: str | None,
        request: ModelRequest,
        on_delta: Callable[[str], None],
    ) -> ModelResponse:
        model = self._resolve_model(provider, model_id)
        request = replace(request, model=model.provider_model_id)
        request, real_by_alias = _alias_request_tools(provider.endpoint, request)
        adapter = self.adapter(provider)
        response = adapter.invoke_stream(
            request, self._providers.resolve_secret(provider), provider.endpoint, on_delta
        )
        return _unalias_response(response, real_by_alias)

    def _adapter_invoke(self, provider: ProviderRecord, request: ModelRequest) -> ModelResponse:
        adapter = self.adapter(provider)
        return adapter.invoke(request, self._providers.resolve_secret(provider), provider.endpoint)
