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
        return self.router.capabilities(self.provider)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        return self.router.invoke(self.provider, self.model_id, request)

    def invoke_stream(
        self, request: ModelRequest, on_delta: Callable[[str], None]
    ) -> ModelResponse:
        return self.router.invoke_stream(self.provider, self.model_id, request, on_delta)
