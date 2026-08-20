"""Scripted model caller: deterministic ModelCaller stand-in for evals.

Implements the same surface AgentLoop consumes (invoke / invoke_stream /
capabilities). The queue is shared with subagent sessions (they reuse the
parent caller), so the sequence must account for subagent invocations.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rinari.models.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
    ToolCall,
    Usage,
)


class ScriptExhaustedError(RuntimeError):
    """The scripted model ran out of responses before the turn finished."""


@dataclass(frozen=True)
class UsageStub:
    output: Usage = field(default_factory=lambda: Usage(input_tokens=10, output_tokens=5))


def answer(content: str, *, usage: Usage | None = None) -> ModelResponse:
    return ModelResponse(
        content=content, stop_reason=StopReason.END_TURN, usage=usage or UsageStub().output
    )


def calls(
    *tool_calls: tuple[str, dict[str, Any]],
    id_prefix: str = "t",
    usage: Usage | None = None,
) -> ModelResponse:
    calls_ = tuple(
        ToolCall(id=f"{id_prefix}{i}-{name}", name=name, arguments=args)
        for i, (name, args) in enumerate(tool_calls)
    )
    return ModelResponse(
        content="",
        tool_calls=calls_,
        stop_reason=StopReason.TOOL_CALLS,
        usage=usage or UsageStub().output,
    )


class ScriptedModel:
    """Deterministic caller, optionally split into named lanes.

    Lanes make parent/subagent interleaving deterministic: each request is
    routed to a lane (by the case's route function) that owns its own queue.
    """

    def __init__(
        self,
        responses: list[ModelResponse] | dict[str, list[ModelResponse]],
        *,
        window: int | None = None,
        route: Callable[[ModelRequest], str] | None = None,
    ):
        if isinstance(responses, dict):
            self._lanes: dict[str, list[ModelResponse]] = {k: list(v) for k, v in responses.items()}
            self._route = route or (lambda _r: "main")
        else:
            self._lanes = {"main": list(responses)}
            self._route = None
        self._lock = threading.Lock()
        self.request_count = 0
        self.requests: list[ModelRequest] = []
        self.lane_hits: dict[str, int] = {}
        self._window = window or 32_000

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=False, tool_calls=True, max_context_tokens=self._window
        )

    def lane(self, request: ModelRequest) -> str:
        lane = self._route(request) if self._route is not None else "main"
        return lane if lane in self._lanes else "main"

    def invoke(self, request: ModelRequest) -> ModelResponse:
        lane = self.lane(request)
        with self._lock:
            self.request_count += 1
            self.requests.append(request)
            self.lane_hits[lane] = self.lane_hits.get(lane, 0) + 1
            queue = self._lanes.get(lane, [])
            if not queue:
                raise ScriptExhaustedError(
                    f"scripted model lane {lane!r} exhausted "
                    f"after {self.lane_hits[lane]} request(s)"
                )
            return queue.pop(0)

    def invoke_stream(
        self, request: ModelRequest, on_delta: Callable[[str], None]
    ) -> ModelResponse:
        response = self.invoke(request)
        if response.content:
            on_delta(response.content)
        return response

    @property
    def remaining(self) -> int:
        with self._lock:
            return sum(len(q) for q in self._lanes.values())
