"""Model router (harness.md section 82).

Phase-2 implementation: manually selected provider/model only (no
auto-routing). The router takes already-resolved provider and model
references, resolves the credential, and dispatches the normalized
`ModelRequest` to the matching adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx

from rinari.providers.adapters.http import (
    is_opencode_endpoint,
    needs_tool_aliasing,
    sanitize_tool_name,
)
from rinari.providers.catalog import OPENCODE_RESPONSES_MODELS
from rinari.providers.errors import invoke_with_retry
from rinari.providers.registry import adapter_for
from rinari.shared.errors import InvalidUsageError

if TYPE_CHECKING:
    from rinari.application.model_service import ModelService
    from rinari.application.provider_service import ProviderService
    from rinari.models.types import ModelRequest, ModelResponse, ProviderCapabilities
    from rinari.storage.records import ProviderRecord


def _resolve_transport(provider, model) -> str:
    """chat (/chat/completions) vs responses (/responses) wire transport.

    An explicit per-model setting wins; otherwise OpenCode catalog IDs on
    OpenCode endpoints default to responses; everything else is chat.
    """
    transport = (model.settings or {}).get("transport")
    if transport is None:
        if (
            is_opencode_endpoint(provider.endpoint)
            and model.provider_model_id in OPENCODE_RESPONSES_MODELS
        ):
            return "responses"
        return "chat"
    if transport not in ("chat", "responses"):
        raise InvalidUsageError(
            f"unknown transport {transport!r} on model {model.alias!r}",
            hint="Expected one of: chat, responses.",
        )
    return transport


def _alias_map_for_request(endpoint: str | None, request: ModelRequest) -> dict[str, str] | None:
    """Alias->real tool-name map for vendors with strict name patterns.

    None off-vendor (or tool-less): adapters then pass names through
    untouched. The router applies the same map back on the way in.
    """
    if not request.tools or not needs_tool_aliasing(endpoint):
        return None
    real_by_alias: dict[str, str] = {}
    for tool in sorted(request.tools, key=lambda t: t.name):
        base = sanitize_tool_name(tool.name)
        alias, n = base, 2
        while alias in real_by_alias and real_by_alias[alias] != tool.name:
            alias = f"{base}__{n}"
            n += 1
        real_by_alias[alias] = tool.name
    return real_by_alias


def _merge_capabilities(
    base: ProviderCapabilities, override: dict[str, Any] | None
) -> ProviderCapabilities:
    """Per-model capability matrix (§6.1): record overrides adapter defaults.

    Unknown keys are ignored; mistyped values fall back to the adapter
    value instead of crashing capability resolution.
    """
    if not override:
        return base
    fields = {
        "streaming": bool,
        "tool_calls": bool,
        "structured_output": bool,
        "max_context_tokens": int,
        "reasoning_effort": bool,
        "vision": bool,
    }
    changes: dict[str, Any] = {}
    for key, kind in fields.items():
        if key not in override:
            continue
        value = override[key]
        if value is None or isinstance(value, kind):
            changes[key] = value
    return replace(base, **changes) if changes else base


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

    # Normalized capability matrix keys (docs/desktop 03-B). Engine-wide
    # contract: tools/streaming/structured_output/reasoning mirror the
    # merged ProviderCapabilities; vision is None until an adapter can
    # determine it (unknown, never an invented False); max_context_window
    # is None when no source states it.
    MATRIX_KEYS = (
        "tools",
        "streaming",
        "structured_output",
        "reasoning",
        "vision",
        "max_context_window",
    )

    def capability_matrix(self, provider: ProviderRecord, model_id: str) -> dict[str, Any]:
        """Normalized map + derived routing signals for one model."""
        merged = self.capabilities(provider, model_id)
        matrix = {
            "tools": merged.tool_calls,
            "streaming": merged.streaming,
            "structured_output": merged.structured_output,
            "reasoning": merged.reasoning_effort,
            "vision": merged.vision,
            "max_context_window": merged.max_context_tokens,
        }
        levels = self.reasoning_levels(self._models.resolve(model_id))
        if levels is not None:
            matrix["reasoning_levels"] = levels
        return {
            "capabilities": matrix,
            "supports_tools": bool(matrix["tools"]),
            "unknown": sorted(key for key, value in matrix.items() if value is None),
        }

    def capabilities(
        self, provider: ProviderRecord, model_id: str | None = None
    ) -> ProviderCapabilities:
        base = self.adapter(provider).capabilities()
        if model_id is None:
            return base
        try:
            model = self._models.resolve(model_id)
        except Exception:
            return base
        if model.provider_id != provider.id:
            return base
        return _merge_capabilities(base, model.capabilities)

    @staticmethod
    def reasoning_levels(model):
        metadata = model.capabilities or {}
        levels = metadata.get("reasoning_levels")
        if levels is None and isinstance(metadata.get("reasoning"), dict):
            levels = metadata["reasoning"].get("supported_efforts")
        return levels if isinstance(levels, list) else None

    def _validate_reasoning(self, provider, model, request):
        effort = request.reasoning_effort
        if effort is None:
            return
        levels = self.reasoning_levels(model)
        if not self.capabilities(provider, model.id).reasoning_effort:
            raise InvalidUsageError("This model does not support configurable reasoning.")
        if levels is not None and effort not in levels:
            raise InvalidUsageError(
                f"Reasoning level {effort!r} is not supported by model {model.alias!r}.",
                hint=f"Supported levels: {', '.join(levels)}",
            )

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
        self._validate_reasoning(provider, model, request)
        real_by_alias = _alias_map_for_request(provider.endpoint, request)
        transport = _resolve_transport(provider, model)
        return _unalias_response(
            self._adapter_invoke(provider, request, transport, real_by_alias), real_by_alias
        )

    def invoke_stream(
        self,
        provider: ProviderRecord,
        model_id: str | None,
        request: ModelRequest,
        on_delta: Callable[[str], None],
    ) -> ModelResponse:
        model = self._resolve_model(provider, model_id)
        request = replace(request, model=model.provider_model_id)
        self._validate_reasoning(provider, model, request)
        real_by_alias = _alias_map_for_request(provider.endpoint, request)
        transport = _resolve_transport(provider, model)
        adapter = self.adapter(provider)
        response = adapter.invoke_stream(
            request,
            self._providers.resolve_secret(provider),
            provider.endpoint,
            on_delta,
            transport=transport,
            tool_aliases=real_by_alias,
        )
        return _unalias_response(response, real_by_alias)

    def _adapter_invoke(
        self,
        provider: ProviderRecord,
        request: ModelRequest,
        transport: str = "chat",
        tool_aliases: dict[str, str] | None = None,
    ) -> ModelResponse:
        adapter = self.adapter(provider)
        secret = self._providers.resolve_secret(provider)
        # §6.4: transient model-call failures retry with backoff. Streaming
        # deliberately does NOT retry: partial deltas already delivered make
        # the response ambiguous.
        return invoke_with_retry(
            lambda: adapter.invoke(
                request,
                secret,
                provider.endpoint,
                transport=transport,
                tool_aliases=tool_aliases,
            )
        )
