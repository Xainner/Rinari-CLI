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
from rinari.runtime.budget import NETWORK_CALLS as NETWORK_CALLS_DIM
from rinari.runtime.budget import TOOL_CALLS as TOOL_CALLS_DIM
from rinari.runtime.budget import BudgetMeter
from rinari.runtime.cancellation import CancellationToken
from rinari.runtime.loopdetection import LoopDetector
from rinari.shared.errors import CancelledError
from rinari.tools.definition import ToolContext, ToolErrorCode, ToolErrorInfo, ToolResult
from rinari.tools.runtime import ToolRuntime
from rinari.tools.scheduler import schedule

# Event names persist verbatim into session_events (trace base, phase 2).
EVENT_TURN_STARTED = "AgentTurnStarted"
EVENT_MODEL_INVOKED = "ModelInvoked"
EVENT_TOOL_COMPLETED = "ToolCompleted"
EVENT_TURN_COMPLETED = "AgentTurnCompleted"
EVENT_LOOP_DETECTED = "LoopDetected"

DEFAULT_MAX_MODEL_CALLS = 8
DEFAULT_MAX_TOOL_CALLS = 32


@dataclass(frozen=True, slots=True)
class TurnResult:
    kind: str  # answer | truncated | cancelled | error | max_model_calls | budget | loop
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
    # Turn budgets (phase 4): final meter snapshot when a budget was active.
    budget: dict | None = None


# `model_provider` exposes: capabilities() -> ProviderCapabilities,
# invoke(request) -> ModelResponse, invoke_stream(request, on_delta) -> ModelResponse.
ModelProvider = Any
DeltaFn = Callable[[str], None]
ToolHook = Callable[[str, str, object], None]  # (phase, name, detail)
# (agent_ctx, pressure, used_input_tokens, window_tokens); storage-aware
# compaction lives outside the loop and mutates AgentContext in place.
PressureHook = Callable[[object, float, int, int], None]
# (event, payload) -> None; lifecycle hook sink (hooks engine / trace).
HookSink = Callable[[str, dict], None]


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
        hook_sink: HookSink | None = None,
    ) -> None:
        self._provider = model_provider
        self._tools = tool_runtime
        self._assembler = assembler
        self._max_model_calls = max_model_calls
        self._max_tool_calls = max_tool_calls
        self._event_sink = event_sink
        self._on_pressure = on_pressure
        self._hook_sink = hook_sink

    @property
    def tool_registry(self):
        return self._tools.registry

    # -- public API ---------------------------------------------------------

    def turn(
        self,
        ctx: AgentContext,
        user_message: str,
        *,
        on_delta: DeltaFn | None = None,
        on_tool: ToolHook | None = None,
        cancel: CancellationToken | None = None,
        budget: BudgetMeter | None = None,
        loop: LoopDetector | None = None,
        turn_index: int | None = None,
    ) -> TurnResult:
        cancel = cancel if cancel is not None else CancellationToken()
        loop = loop if loop is not None else LoopDetector()
        started_payload: dict = {"preview": user_message[:200]}
        if turn_index is not None:
            started_payload["turn_index"] = turn_index
        self._emit(ctx.session_id, EVENT_TURN_STARTED, started_payload)
        ctx.history.append(ChatMessage.user(user_message))
        # Hierarchical ledger (P0.10): the turn's meter becomes the parent
        # budget visible to agent.spawn, so subagent cost aggregates here.
        if (
            budget is not None
            and isinstance(ctx.tool_ctx, ToolContext)
            and ctx.tool_ctx.parent_budget is not budget
        ):
            ctx.tool_ctx = replace(ctx.tool_ctx, parent_budget=budget)
        tool_calls_requested = 0
        tool_calls_executed = 0
        tool_calls_rejected = 0
        tool_seq = 0
        total_usage: Usage | None = None

        # A present meter is authoritative for model-call iterations; the
        # loop's own cap is only a defensive fallback without one.
        max_iters = budget.limits.max_model_calls if budget is not None else self._max_model_calls
        for _ in range(max_iters):
            cancel.throw_if_cancelled()
            if budget is not None:
                hit = budget.first_exhausted(ignore=(TOOL_CALLS_DIM, NETWORK_CALLS_DIM))
                if hit is not None:
                    return self._stop(
                        ctx.session_id,
                        "budget",
                        f"Stopped: turn budget exhausted ({hit}: {_budget_reason(hit)}).",
                        tool_calls_executed,
                        total_usage,
                        budget,
                        turn_index,
                        requested=tool_calls_requested,
                        rejected=tool_calls_rejected,
                    )
                if budget.model_calls >= budget.limits.max_model_calls:
                    return self._stop(
                        ctx.session_id,
                        "budget",
                        "Stopped: turn budget exhausted (model-calls).",
                        tool_calls_executed,
                        total_usage,
                        budget,
                        turn_index,
                        requested=tool_calls_requested,
                        rejected=tool_calls_rejected,
                    )
                budget.note_model_call()
            request = self._build_request(ctx)
            before_model_payload: dict = {
                "model": ctx.model_ref,
                "messages": len(request.messages),
                "tools": len(request.tools),
            }
            exposure_metrics = getattr(ctx.tool_ctx, "exposure", None)
            if exposure_metrics is not None:
                before_model_payload["exposure"] = exposure_metrics.metrics(self._tools.registry)
            self._emit_hook("BeforeModel", before_model_payload)
            response = self._invoke(ctx, request, self._guarded_delta(ctx, on_delta))
            total_usage = _merge_usage(total_usage, response.usage)
            self._emit_hook(
                "AfterModel",
                {
                    "stop_reason": response.stop_reason.value,
                    "tool_calls": len(response.tool_calls),
                    "usage": _usage_dict(response.usage),
                },
            )
            if budget is not None:
                budget.note_usage(response.usage)
            self._emit(
                ctx.session_id,
                EVENT_MODEL_INVOKED,
                {
                    "stop_reason": response.stop_reason.value,
                    "tool_calls": [{"id": tc.id, "name": tc.name} for tc in response.tool_calls],
                    "usage": _usage_dict(response.usage),
                    # Etapa C: execution plan groups (serial baseline; the
                    # plan is traced so a concurrent executor can adopt it).
                    "execution_plan": schedule(
                        [tc.name for tc in response.tool_calls], self._tools.registry
                    ),
                },
            )
            self._check_pressure(ctx, response)

            if not response.has_tool_calls:
                ctx.history.append(ChatMessage.assistant(response.content or ""))
                kind = "truncated" if response.stop_reason is StopReason.MAX_TOKENS else "answer"
                self._emit_hook(
                    "BeforeFinal", {"kind": kind, "preview": (response.content or "")[:200]}
                )
                self._emit(
                    ctx.session_id,
                    EVENT_TURN_COMPLETED,
                    _turn_completed_payload(
                        kind,
                        tool_calls_executed,
                        budget,
                        turn_index,
                        requested=tool_calls_requested,
                        rejected=tool_calls_rejected,
                    ),
                )
                return TurnResult(
                    kind=kind,
                    content=response.content or "",
                    tool_calls=tool_calls_executed,
                    usage=total_usage,
                    budget=budget.snapshot() if budget is not None else None,
                )

            # EXECUTE: run every requested tool, feed results back as tool msgs.
            ctx.history.append(ChatMessage.assistant(response.content or "", response.tool_calls))
            for call in response.tool_calls:
                cancel.throw_if_cancelled()
                tool_calls_requested += 1
                network = self._call_is_network(call.name, call.arguments)
                if budget is not None:
                    allowed = budget.allows_tool(call.name, is_network=network)
                else:
                    allowed = tool_calls_executed < self._max_tool_calls
                executed = False
                if call.arguments_invalid:
                    # Malformed wire arguments are never executed and never
                    # charged: the model gets a retryable observation instead.
                    result = ToolResult(
                        ok=False,
                        error=ToolErrorInfo(
                            code=ToolErrorCode.INVALID_ARGUMENT,
                            message=(
                                f"Model emitted invalid JSON for arguments of "
                                f"{call.name!r}; fix the arguments and retry"
                            ),
                            retryable=True,
                        ),
                    )
                    self._emit_hook(
                        "ToolError",
                        {
                            "tool": call.name,
                            "tool_call_id": call.id,
                            "code": result.error.code.value,
                            "message": result.error.message,
                        },
                    )
                elif not allowed:
                    tool_calls_rejected += 1
                    result = ToolResult(
                        ok=False,
                        error=ToolErrorInfo(
                            code=ToolErrorCode.RESOURCE_EXHAUSTED,
                            message="per-turn tool budget exhausted; stop calling tools",
                            retryable=False,
                        ),
                    )
                else:
                    executed = True
                    tool_calls_executed += 1
                    if budget is not None:
                        budget.note_tool_call(call.name, is_network=network)
                    exposure_used = getattr(ctx.tool_ctx, "exposure", None)
                    if exposure_used is not None:
                        # Executed tools stay visible for continuity (Etapa B).
                        exposure_used.note_used(call.name)
                    self._emit_hook(
                        "PreToolUse",
                        {"tool": call.name, "arguments": call.arguments, "tool_call_id": call.id},
                    )
                    _hook(on_tool, "start", call.name, call.arguments)
                    trace = {"tool_seq": tool_seq + 1}
                    if turn_index is not None:
                        trace["turn_index"] = turn_index
                    result = self._tools.execute(
                        call.name,
                        call.arguments,
                        ctx.tool_ctx,
                        tool_call_id=call.id,
                        trace=trace,
                    )
                    self._emit_hook(
                        "PostToolUse",
                        {
                            "tool": call.name,
                            "tool_call_id": call.id,
                            "ok": result.ok,
                            "duration_ms": round(result.duration_ms, 1),
                            "truncated": result.truncated,
                        },
                    )
                    if result.error is not None:
                        self._emit_hook(
                            "ToolError",
                            {
                                "tool": call.name,
                                "tool_call_id": call.id,
                                "code": result.error.code.value,
                                "message": result.error.message,
                            },
                        )
                    _hook(on_tool, "end", call.name, result)
                tool_seq += 1
                ctx.history.append(
                    ChatMessage.tool_result(call.id, call.name, result.to_model_text(call.name))
                )
                if not executed:
                    tool_completed_payload: dict = {
                        "tool_call_id": call.id,
                        "name": call.name,
                        "ok": result.ok,
                        "error_code": result.error.code.value if result.error else None,
                        "duration_ms": round(result.duration_ms, 1),
                        "truncated": result.truncated,
                        "artifacts": [a.uri for a in result.artifacts],
                        "tool_seq": tool_seq,
                    }
                    if turn_index is not None:
                        tool_completed_payload["turn_index"] = turn_index
                    self._emit(ctx.session_id, EVENT_TOOL_COMPLETED, tool_completed_payload)
                loop.record_tool(call.name, call.arguments)
                if result.error is not None:
                    loop.record_error(call.name, result.error.code.value, result.error.message)
                signal = loop.check()
                if signal is not None:
                    self._emit(
                        ctx.session_id,
                        EVENT_LOOP_DETECTED,
                        {"kind": signal.kind, "detail": signal.detail, "action": signal.action},
                    )
                    if signal.action == "stop":
                        return self._stop(
                            ctx.session_id,
                            "loop",
                            f"Stopped: loop detected ({signal.kind} — {signal.detail}).",
                            tool_calls_executed,
                            total_usage,
                            budget,
                            turn_index,
                            requested=tool_calls_requested,
                            rejected=tool_calls_rejected,
                        )
                    ctx.history.append(ChatMessage.user(loop.nudge_text(signal)))

            if budget is not None:
                hit = budget.first_exhausted(ignore=(TOOL_CALLS_DIM, NETWORK_CALLS_DIM))
                if hit is not None:
                    return self._stop(
                        ctx.session_id,
                        "budget",
                        f"Stopped: turn budget exhausted ({hit}: {_budget_reason(hit)}).",
                        tool_calls_executed,
                        total_usage,
                        budget,
                        turn_index,
                        requested=tool_calls_requested,
                        rejected=tool_calls_rejected,
                    )

        return self._stop(
            ctx.session_id,
            "budget",
            "Stopped: exceeded the per-turn model-call limit while the model kept "
            "requesting tools.",
            tool_calls_executed,
            total_usage,
            budget,
            turn_index,
            requested=tool_calls_requested,
            rejected=tool_calls_rejected,
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
        exposure = getattr(ctx.tool_ctx, "exposure", None)
        if exposure is not None:
            # Dynamic exposure (Etapa B): core + activated + recent, pruned
            # once per model request so turn-scoped activations decay.
            exposure.prune()
            wire_tools = exposure.for_model(self._tools.registry)
        else:
            wire_tools = self._tools.registry.for_model()
        return ModelRequest(
            model=ctx.model_ref,
            messages=tuple(messages),
            tools=wire_tools,
            session_id=ctx.session_id,
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

    def _guarded_delta(self, ctx: AgentContext, on_delta: DeltaFn | None) -> DeltaFn | None:
        """Abort a live stream promptly on cancel (§8/Etapa D).

        The guard raises inside the adapter's stream loop, which unwinds
        through the httpx stream context (closed on exception) instead of
        waiting for the network timeout.
        """
        if on_delta is None:
            return None
        cancellation = getattr(getattr(ctx, "tool_ctx", None), "cancellation", None)
        if cancellation is None:
            return on_delta

        def guarded(text: str) -> None:
            cancellation.throw_if_cancelled()
            on_delta(text)

        return guarded

    def _call_is_network(self, name: str, arguments: Any) -> bool:
        """Ground-truth network classification for budget dimensions.

        Uses the tool registry + declared capabilities, never the name
        prefix. Unknown tools and classification failures are not network.
        """
        try:
            definition = self._tools.registry.get(name)
        except Exception:
            return False
        if definition is None:
            return False
        try:
            args = arguments if isinstance(arguments, dict) else {}
            return definition.classify_action(args).capability == "network.outbound"
        except Exception:
            return False

    def _stop(
        self,
        session_id: str,
        kind: str,
        content: str,
        tool_calls: int,
        usage: Usage | None,
        budget: BudgetMeter | None = None,
        turn_index: int | None = None,
        *,
        requested: int = 0,
        rejected: int = 0,
    ) -> TurnResult:
        self._emit(
            session_id,
            EVENT_TURN_COMPLETED,
            _turn_completed_payload(
                kind, tool_calls, budget, turn_index, requested=requested, rejected=rejected
            ),
        )
        return TurnResult(
            kind=kind,
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            budget=budget.snapshot() if budget is not None else None,
        )

    def _emit(self, session_id: str, event_type: str, payload: dict) -> None:
        if self._event_sink is None:
            return
        with contextlib.suppress(Exception):
            # Observability must never take the conversation down.
            self._event_sink(session_id, event_type, payload)

    def _emit_hook(self, event: str, payload: dict) -> None:
        if self._hook_sink is None:
            return
        with contextlib.suppress(Exception):
            # Hook failures are recorded in the outcome; never fatal here.
            self._hook_sink(event, payload)


def _turn_completed_payload(
    kind: str,
    tool_calls: int,
    budget: BudgetMeter | None,
    turn_index: int | None = None,
    *,
    requested: int = 0,
    rejected: int = 0,
) -> dict:
    payload = {
        "kind": kind,
        "tool_calls": tool_calls,
        "tool_calls_requested": requested,
        "tool_calls_rejected": rejected,
    }
    if budget is not None:
        payload["budget"] = budget.snapshot()
    if turn_index is not None:
        payload["turn_index"] = turn_index
    return payload


_BUDGET_REASONS = {
    "model-calls": "model-call limit reached",
    "tool-calls": "tool-call limit reached",
    "network-calls": "network-call limit reached",
    "subagents": "subagent limit reached",
    "recursion-depth": "recursion-depth limit reached",
    "cost": "cost limit reached",
    "wall-time": "wall-time limit reached",
}


def _budget_reason(name: str) -> str:
    return _BUDGET_REASONS.get(name, name)


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
