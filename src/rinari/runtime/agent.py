"""Agent loop core: the autonomous model/tool conversation driver.

Implements the phase-2 loop (harness.md sections 53-54):

    RECEIVE -> ORIENT -> PLAN -> EXECUTE -> OBSERVE -> EVALUATE

The loop is provider-agnostic (harness.md section 28): conversation state is
plain ChatMessage records, never provider thread/response IDs. It is also
storage-agnostic: events leave through an injected sink (the CLI wires it to
the session event store), so this module stays unit-testable with fakes.

Control flow is intentionally boring: a bounded while loop that keeps
invoking the model while it asks for tool calls, feeding normalized tool
results back as `tool` messages. Cancellation is cooperative: the token is
checked before every model call and before every tool call; a Ctrl+C during
model I/O surfaces as a CancelledError at the next boundary.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from rinari.context.tokens import (
    DEFAULT_CONTEXT_WINDOW,
    PRESSURE_COMPACT,
    estimate_tokens,
    pressure,
)
from rinari.models.types import ChatMessage, ModelRequest, StopReason, Usage
from rinari.prompts.assembler import AssemblerContext, PromptAssembler
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.errors import CancelledError
from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult
from rinari.tools.runtime import ToolRuntime

# Event names persist verbatim into session_events (trace base, phase 2).
EVENT_TURN_STARTED = "AgentTurnStarted"
EVENT_MODEL_INVOKED = "ModelInvoked"
EVENT_TOOL_COMPLETED = "ToolCompleted"
EVENT_TURN_COMPLETED = "AgentTurnCompleted"

DEFAULT_MAX_MODEL_CALLS = 8
DEFAULT_MAX_TOOL_CALLS = 32


@dataclass(frozen=True, slots=True)
class TurnResult:
    kind: str  # answer | truncated | cancelled | error | max_model_calls | budget
    content: str
    tool_calls: int
    usage: Usage | None
    # Finalize transition (phase 3): completion-gate outcome for the turn, as
    # decided by the harness from persisted validation evidence (or None when
    # no gate was evaluated, e.g. CHAT sessions or turns without tool calls).
    completion: dict | None = None
    # Context compaction (phase 4): True when this turn triggered a
    # provider-independent compaction of the in-memory context.
    compacted: bool = False


# `model_provider` exposes: capabilities() -> ProviderCapabilities,
# invoke(request) -> ModelResponse, invoke_stream(request, on_delta) -> ModelResponse.
ModelProvider = Any
DeltaFn = Callable[[str], None]
ToolHook = Callable[[str, str, object], None]  # (phase, name, detail)
# (agent_ctx, pressure, used_input_tokens, window_tokens); storage-aware
# compaction lives outside the loop and mutates AgentContext in place.
PressureHook = Callable[[object, float, int, int], None]


@dataclass(slots=True)
class AgentContext:
    """Per-session mutable state owned by the loop (not by any provider)."""

    session_id: str
    model_ref: str
    tool_ctx: Any  # tools.definition.ToolContext
    assembler_base: AssemblerContext  # stable segments; history injected per turn
    history: list[ChatMessage] = field(default_factory=list)
    # Context compaction (phase 4): preserved task truth rendered for the
    # system prompt; `dropped_total` counts history messages trimmed in
    # memory since the last persistence (the persisted conversation itself
    # is never truncated); `compacted` signals a fresh compaction to the
    # session host for user/JSON notification.
    compact_state_text: str | None = None
    dropped_total: int = 0
    compacted: bool = False


class AgentLoop:
    def __init__(
        self,
        model_provider: ModelProvider,
        tool_runtime: ToolRuntime,
        assembler: PromptAssembler,
        *,
        max_model_calls: int = DEFAULT_MAX_MODEL_CALLS,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        event_sink: Callable[[str, str, dict], None] | None = None,
        on_pressure: PressureHook | None = None,
    ) -> None:
        self._provider = model_provider
        self._tools = tool_runtime
        self._assembler = assembler
        self._max_model_calls = max_model_calls
        self._max_tool_calls = max_tool_calls
        self._event_sink = event_sink
        self._on_pressure = on_pressure

    # -- public API ---------------------------------------------------------

    def turn(
        self,
        ctx: AgentContext,
        user_message: str,
        *,
        on_delta: DeltaFn | None = None,
        on_tool: ToolHook | None = None,
        cancel: CancellationToken | None = None,
    ) -> TurnResult:
        cancel = cancel if cancel is not None else CancellationToken()
        self._emit(ctx.session_id, EVENT_TURN_STARTED, {"preview": user_message[:200]})
        ctx.history.append(ChatMessage.user(user_message))
        tool_calls_made = 0
        total_usage: Usage | None = None

        for _ in range(self._max_model_calls):
            cancel.throw_if_cancelled()
            request = self._build_request(ctx)
            response = self._invoke(ctx, request, on_delta)
            total_usage = _merge_usage(total_usage, response.usage)
            self._emit(
                ctx.session_id,
                EVENT_MODEL_INVOKED,
                {
                    "stop_reason": response.stop_reason.value,
                    "tool_calls": [{"id": tc.id, "name": tc.name} for tc in response.tool_calls],
                    "usage": _usage_dict(response.usage),
                },
            )
            self._check_pressure(ctx, response)

            if not response.has_tool_calls:
                ctx.history.append(ChatMessage.assistant(response.content or ""))
                kind = "truncated" if response.stop_reason is StopReason.MAX_TOKENS else "answer"
                self._emit(
                    ctx.session_id,
                    EVENT_TURN_COMPLETED,
                    {"kind": kind, "tool_calls": tool_calls_made},
                )
                return TurnResult(
                    kind=kind,
                    content=response.content or "",
                    tool_calls=tool_calls_made,
                    usage=total_usage,
                )

            # EXECUTE: run every requested tool, feed results back as tool msgs.
            ctx.history.append(ChatMessage.assistant(response.content or "", response.tool_calls))
            for call in response.tool_calls:
                cancel.throw_if_cancelled()
                if tool_calls_made >= self._max_tool_calls:
                    result = ToolResult(
                        ok=False,
                        error=ToolErrorInfo(
                            code=ToolErrorCode.TIMEOUT,
                            message="per-turn tool budget exhausted; stop calling tools",
                            retryable=False,
                        ),
                    )
                else:
                    _hook(on_tool, "start", call.name, call.arguments)
                    result = self._tools.execute(
                        call.name, call.arguments, ctx.tool_ctx, tool_call_id=call.id
                    )
                    _hook(on_tool, "end", call.name, result)
                tool_calls_made += 1
                ctx.history.append(
                    ChatMessage.tool_result(call.id, call.name, result.to_model_text())
                )
                self._emit(
                    ctx.session_id,
                    EVENT_TOOL_COMPLETED,
                    {
                        "tool_call_id": call.id,
                        "name": call.name,
                        "ok": result.ok,
                        "error_code": result.error.code.value if result.error else None,
                        "duration_ms": round(result.duration_ms, 1),
                        "truncated": result.truncated,
                        "artifacts": [a.uri for a in result.artifacts],
                    },
                )

        return self._stop(
            ctx.session_id,
            "budget",
            "Stopped: exceeded the per-turn model-call limit while the model kept "
            "requesting tools.",
            tool_calls_made,
            total_usage,
        )

    # -- internals ------------------------------------------------------------

    def _build_request(self, ctx: AgentContext) -> ModelRequest:
        context = replace(
            ctx.assembler_base,
            history=tuple(ctx.history),
            compact_state=ctx.compact_state_text,
        )
        bundle = self._assembler.build(context)
        messages: list[ChatMessage] = []
        if bundle.system_prompt:
            messages.append(ChatMessage.system(bundle.system_prompt))
        messages.extend(ctx.history)
        return ModelRequest(
            model=ctx.model_ref,
            messages=tuple(messages),
            tools=self._tools.registry.for_model(),
        )

    def _check_pressure(self, ctx: AgentContext, response: Any) -> None:
        """Context-pressure hook (phase 4): never takes the turn down."""
        if self._on_pressure is None:
            return
        try:
            window = (
                getattr(self._provider.capabilities(), "max_context_tokens", None)
                or DEFAULT_CONTEXT_WINDOW
            )
            used = response.usage.input_tokens or estimate_tokens(history=ctx.history)
            pressure_value = pressure(used, window)
            if pressure_value is not None and pressure_value >= PRESSURE_COMPACT:
                ctx.compacted = False
                self._on_pressure(ctx, pressure_value, used, window)
        except Exception:
            pass

    def _invoke(self, ctx: AgentContext, request: ModelRequest, on_delta: DeltaFn | None) -> Any:
        capabilities = self._provider.capabilities()
        if on_delta is not None and getattr(capabilities, "streaming", False):
            return self._provider.invoke_stream(request, on_delta)
        return self._provider.invoke(request)

    def _stop(
        self,
        session_id: str,
        kind: str,
        content: str,
        tool_calls: int,
        usage: Usage | None,
    ) -> TurnResult:
        self._emit(session_id, EVENT_TURN_COMPLETED, {"kind": kind, "tool_calls": tool_calls})
        return TurnResult(kind=kind, content=content, tool_calls=tool_calls, usage=usage)

    def _emit(self, session_id: str, event_type: str, payload: dict) -> None:
        if self._event_sink is None:
            return
        with contextlib.suppress(Exception):
            # Observability must never take the conversation down.
            self._event_sink(session_id, event_type, payload)


def _hook(on_tool: ToolHook | None, phase: str, name: str, detail: object) -> None:
    if on_tool is None:
        return
    with contextlib.suppress(Exception):
        on_tool(phase, name, detail)


def _merge_usage(prev: Usage | None, new: Usage | None) -> Usage | None:
    if new is None and (prev is None or (prev.input_tokens is None and prev.output_tokens is None)):
        return prev
    if new is None:
        return prev
    if prev is None or (prev.input_tokens is None and prev.output_tokens is None):
        return new

    def _sum(a: int | None, b: int | None) -> int | None:
        if a is None or b is None:
            return None
        return a + b

    return Usage(
        input_tokens=_sum(prev.input_tokens, new.input_tokens),
        output_tokens=_sum(prev.output_tokens, new.output_tokens),
        cached_input_tokens=_sum(prev.cached_input_tokens, new.cached_input_tokens),
        reasoning_tokens=_sum(prev.reasoning_tokens, new.reasoning_tokens),
    )


def _usage_dict(usage: Usage | None) -> dict | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }


__all__ = [
    "EVENT_MODEL_INVOKED",
    "EVENT_TOOL_COMPLETED",
    "EVENT_TURN_COMPLETED",
    "EVENT_TURN_STARTED",
    "AgentContext",
    "AgentLoop",
    "CancelledError",
    "TurnResult",
]
