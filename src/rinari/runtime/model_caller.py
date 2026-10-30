"""ModelCaller: the narrow interface the AgentLoop consumes for model I/O.

Wraps a ModelRouter + a concrete provider/model selection so the loop never
touches application services or storage directly. Swapping the provider or
model mid-session means building a *new* ModelCaller; the conversation
history (plain ChatMessage records) is untouched, which is what keeps the
session state provider-independent (harness.md section 28).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rinari.models.router import ModelRouter
from rinari.models.types import ModelRequest, ModelResponse, ProviderCapabilities
from rinari.storage.records import ProviderRecord


@dataclass(frozen=True, slots=True)
class ModelCaller:
    router: ModelRouter
    provider: ProviderRecord
    model_id: str | None

    def capabilities(self) -> ProviderCapabilities:
        return self.router.capabilities(self.provider, self.model_id)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        return self.router.invoke(self.provider, self.model_id, request)

    def invoke_stream(
        self, request: ModelRequest, on_delta: Callable[[str], None]
    ) -> ModelResponse:
        return self.router.invoke_stream(self.provider, self.model_id, request, on_delta)


class SessionModelGateway:
    """Stable indirection between AgentLoop and the active ModelCaller.

    The loop holds the gateway for the whole session; provider/model
    switches swap ``current`` so later turns use the new caller while the
    conversation history (plain ChatMessage records) is untouched. Never
    fan out or cache: every call delegates to exactly one current caller.
    """

    def __init__(self, current: ModelCaller) -> None:
        self._current = current
        self._primary = current
        # The fallback model's caller, built on demand (plan 0.4.0 #80).
        self.fallback_factory: Callable[[], ModelCaller | None] | None = None
        self.on_fallback = False

    @property
    def current(self) -> ModelCaller:
        return self._current

    def switch(self, caller: ModelCaller) -> None:
        for name in ("activity_sink", "event_sink", "token", "budget_getter"):
            if hasattr(self._current, name) and hasattr(caller, name):
                setattr(caller, name, getattr(self._current, name))
        self._current = caller
        self._primary = caller
        self.on_fallback = False

    def use_fallback(self) -> ModelCaller | None:
        """Switch this turn to the fallback model; None when there is none."""
        if self.on_fallback or self.fallback_factory is None:
            return None
        try:
            caller = self.fallback_factory()
        except Exception:
            return None
        if caller is None:
            return None
        primary = self._primary
        for name in ("activity_sink", "event_sink", "token", "budget_getter"):
            if hasattr(primary, name) and hasattr(caller, name):
                setattr(caller, name, getattr(primary, name))
        self._current = caller
        self.on_fallback = True
        return caller

    def restore_primary(self) -> None:
        """A new turn tries the conversation's own model again."""
        self._current = self._primary
        self.on_fallback = False

    @property
    def primary_model_id(self) -> str | None:
        return getattr(self._primary, "model_id", None)

    def capabilities(self) -> ProviderCapabilities:
        return self._current.capabilities()

    def visual_decision(self):
        from rinari.runtime.vision import visual_status

        return visual_status(self._current)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        return self._current.invoke(request)

    def invoke_stream(
        self, request: ModelRequest, on_delta: Callable[[str], None]
    ) -> ModelResponse:
        return self._current.invoke_stream(request, on_delta)
