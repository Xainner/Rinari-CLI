"""Model router (harness.md section 82).

Phase-2 implementation: manually selected provider/model only (no
auto-routing). The router takes already-resolved provider and model
references, resolves the credential, and dispatches the normalized
`ModelRequest` to the matching adapter.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx

from rinari.providers.adapters.http import (
    DEFAULT_MODEL_STREAM_READ_TIMEOUT_S,
    MAX_MODEL_STREAM_READ_TIMEOUT_S,
    MIN_MODEL_STREAM_READ_TIMEOUT_S,
    WIRE_TOOL_NAME_RE,
    is_opencode_endpoint,
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

    Activated whenever any requested tool name violates the official
    function-name contract (letters, digits, underscore, hyphen only —
    both OpenAI and Anthropic reject dotted names, F3), regardless of the
    endpoint host. Conforming names keep their exact spelling; the map is
    reversible and collision-safe. The router applies the same map back on
    the way in.
    """
    if not request.tools or all(WIRE_TOOL_NAME_RE.fullmatch(t.name) for t in request.tools):
        return None
    real_by_alias: dict[str, str] = {}
    for tool in sorted(request.tools, key=lambda t: t.name):
        alias = tool.name if WIRE_TOOL_NAME_RE.fullmatch(tool.name) else _wire_alias(tool.name)
        suffix = 2
        while alias in real_by_alias and real_by_alias[alias] != tool.name:
            # Deterministic collision suffix; the budgeted alias keeps the
            # suffixed name inside the wire pattern even at the 64-char bound
            # (review P1: long names were aliased but never shortened).
            alias = f"{_wire_alias(tool.name, reserve=suffix + 2)}__{suffix}"
            suffix += 1
        real_by_alias[alias] = tool.name
    return real_by_alias


def _wire_alias(name: str, *, reserve: int = 0) -> str:
    """Sanitized alias within the official 64-character contract.

    Collision suffixes like ``__12`` can exceed the pattern on their own,
    so `reserve` withholds that many characters from the sanitized base
    before appending them.
    """
    return sanitize_tool_name(name)[: max(0, 64 - reserve)]


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


def _stream_read_timeout_s(
    provider_settings: dict[str, Any], model_settings: dict[str, Any]
) -> float:
    """Resolve a bounded per-provider/model stream inactivity timeout.

    Model settings override provider settings. An installation environment
    fallback is available for deployments that cannot edit persisted records;
    the ordinary default remains 30 seconds.
    """
    raw: Any = None
    source = "default"
    for label, settings in (("provider", provider_settings), ("model", model_settings)):
        if isinstance(settings, dict) and "stream_read_timeout_s" in settings:
            raw = settings.get("stream_read_timeout_s")
            source = f"{label}.stream_read_timeout_s"
    if raw is None:
        raw = os.environ.get("RINARI_MODEL_STREAM_READ_TIMEOUT_SECONDS")
        if raw is not None:
            source = "RINARI_MODEL_STREAM_READ_TIMEOUT_SECONDS"
    if raw is None:
        return DEFAULT_MODEL_STREAM_READ_TIMEOUT_S
    if isinstance(raw, bool):
        raise InvalidUsageError(
            f"{source} must be a number between "
            f"{MIN_MODEL_STREAM_READ_TIMEOUT_S:g} and "
            f"{MAX_MODEL_STREAM_READ_TIMEOUT_S:g} seconds"
        )
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidUsageError(
            f"{source} must be a number between "
            f"{MIN_MODEL_STREAM_READ_TIMEOUT_S:g} and "
            f"{MAX_MODEL_STREAM_READ_TIMEOUT_S:g} seconds"
        ) from exc
    in_bounds = MIN_MODEL_STREAM_READ_TIMEOUT_S <= value <= MAX_MODEL_STREAM_READ_TIMEOUT_S
    if not math.isfinite(value) or not in_bounds:
        raise InvalidUsageError(
            f"{source} must be a number between "
            f"{MIN_MODEL_STREAM_READ_TIMEOUT_S:g} and "
            f"{MAX_MODEL_STREAM_READ_TIMEOUT_S:g} seconds"
        )
    return value


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


def scheduled(method):
    from functools import wraps

    @wraps(method)
    def wrapped(self, provider, model_id, request, *args, **kwargs):
        from rinari.models.execution import destination_slot

        home = getattr(getattr(self._providers, "_ctx", None), "home", None)
        check = request.cancellation.throw_if_cancelled if request.cancellation else lambda: None
        with destination_slot(home, provider.id, check):
            if request.on_dispatched:
                request.on_dispatched()
            return method(self, provider, model_id, request, *args, **kwargs)

    return wrapped


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

    def generation_request(self, provider, model, request):
        from rinari.models.execution import policy

        home = getattr(getattr(self._providers, "_ctx", None), "home", None)
        config = policy(home)
        generation = {
            **(provider.settings or {}).get("generation", {}),
            **(model.settings or {}).get("generation", {}),
        }
        limit = config.get("models", {}).get(model.id, generation.get("max_tokens"))
        if limit is None:
            limit = self.adapter(provider).default_max_tokens
        return replace(
            request,
            model=model.provider_model_id,
            max_tokens=request.max_tokens if request.max_tokens is not None else limit,
            temperature=request.temperature
            if request.temperature is not None
            else generation.get("temperature"),
            reasoning_effort=request.reasoning_effort
            if request.reasoning_effort is not None
            else generation.get("reasoning_effort"),
        )

    @scheduled
    def invoke(
        self,
        provider: ProviderRecord,
        model_id: str | None,
        request: ModelRequest,
    ) -> ModelResponse:
        model = self._resolve_model(provider, model_id)
        request = self.generation_request(provider, model, request)
        self._validate_reasoning(provider, model, request)
        from rinari.models.visual_context import prepare_visual_payload

        constraints = {
            **(provider.settings or {}).get("vision_limits", {}),
            **(model.settings or {}).get("vision_limits", {}),
        }
        request = prepare_visual_payload(request, constraints)
        real_by_alias = _alias_map_for_request(provider.endpoint, request)
        transport = _resolve_transport(provider, model)
        from rinari.models.usage_tracking import observe_call

        return observe_call(
            request,
            lambda _: _unalias_response(
                self._adapter_invoke(provider, request, transport, real_by_alias), real_by_alias
            ),
        )

    @scheduled
    def invoke_stream(
        self,
        provider: ProviderRecord,
        model_id: str | None,
        request: ModelRequest,
        on_delta: Callable[[str], None],
    ) -> ModelResponse:
        model = self._resolve_model(provider, model_id)
        request = self.generation_request(provider, model, request)
        from rinari.models.execution import policy
        from rinari.providers.adapters.http import validate_stream_timeouts

        config = policy(getattr(getattr(self._providers, "_ctx", None), "home", None))
        limits = {
            **config.get("timeouts", {}),
            **config.get("provider_timeouts", {}).get(provider.id, {}),
            **(provider.settings or {}).get("stream_timeouts", {}),
            **(model.settings or {}).get("stream_timeouts", {}),
        }
        legacy = any(
            "stream_read_timeout_s" in (s or {}) for s in (provider.settings, model.settings)
        ) or os.environ.get("RINARI_MODEL_STREAM_READ_TIMEOUT_SECONDS")
        if legacy:
            read = _stream_read_timeout_s(provider.settings, model.settings)
            limits.setdefault("first_byte", read)
            limits.setdefault("idle", read)
        limits = validate_stream_timeouts({**limits, **(request.stream_timeouts or {})})
        request = replace(request, stream_timeouts=limits, stream_read_timeout_s=limits["idle"])
        self._validate_reasoning(provider, model, request)
        from rinari.models.visual_context import prepare_visual_payload

        constraints = {
            **(provider.settings or {}).get("vision_limits", {}),
            **(model.settings or {}).get("vision_limits", {}),
        }
        request = prepare_visual_payload(request, constraints)
        real_by_alias = _alias_map_for_request(provider.endpoint, request)
        transport = _resolve_transport(provider, model)
        adapter = self.adapter(provider)
        from rinari.models.usage_tracking import observe_call

        response = observe_call(
            request,
            lambda delta: adapter.invoke_stream(
                request,
                self._providers.resolve_secret(provider),
                provider.endpoint,
                delta,
                transport=transport,
                tool_aliases=real_by_alias,
            ),
            on_delta,
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
            ),
            attempts=1 if any(message.images for message in request.messages) else 3,
        )
