"""goal.update: Rinari reports how the owner's goal is going (plan 0.4.0 #23).

Only available in a turn of a conversation whose goal is being pursued (the
tool context carries the goal service then). «Achieved» is Rinari's claim;
the goal ends only when the verification gate of that turn agrees.
"""

from __future__ import annotations

import contextlib
from typing import Any

from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

DESCRIPTION = (
    "Report on the goal the user set for this conversation: progress (what this turn did "
    "and what is left), achieved (with the evidence that it works) or blocked (what you "
    "need from the user). Call it once at the end of every goal turn."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary"],
    "properties": {
        "status": {"enum": ["progress", "achieved", "blocked"]},
        "summary": {"type": "string", "maxLength": 600},
        "evidence": {"type": "string", "maxLength": 1200},
        "remaining": {"type": "string", "maxLength": 600},
    },
}


def _fail(code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(ok=False, error=ToolErrorInfo(code=code, message=message, retryable=False))


def _update(arguments: dict, ctx: ToolContext) -> ToolResult:
    from rinari.goals import GoalError

    service = getattr(ctx, "goals", None)
    if service is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "this conversation has no goal")
    arguments = arguments or {}
    try:
        goal = service.report(
            ctx.session_id,
            arguments.get("status"),
            arguments.get("summary"),
            arguments.get("evidence") or "",
            arguments.get("remaining") or "",
        )
    except GoalError as exc:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))
    sink = getattr(ctx, "activity_sink", None)
    if callable(sink):
        with contextlib.suppress(Exception):
            sink("goal.reported", {"goal": goal.as_dict()})
    note = (
        "Recorded. The goal ends when the verification of this turn agrees."
        if arguments.get("status") == "achieved"
        else "Recorded."
    )
    return ToolResult(
        ok=True, data={"state": goal.state, "iteration": goal.iteration, "note": note}
    )


def goal_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="goal.update",
            description=DESCRIPTION,
            input_schema=INPUT_SCHEMA,
            capabilities=("state.mutate",),
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=False,
            handler=_update,
            classify=lambda _input: ClassifiedAction("state.mutate"),
            namespace="goal",
            always_loaded=True,
        )
    ]
