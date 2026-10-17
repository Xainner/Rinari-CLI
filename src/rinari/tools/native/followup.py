"""followup.suggest: a short task Rinari leaves as a note for the owner.

Showing a note never starts anything. The desktop shows it as a sticky note;
accepting it opens a new conversation that starts the task.
"""

from __future__ import annotations

import contextlib
from typing import Any

from rinari.followups import FollowupError
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
    "Leave the user a note with one worthwhile follow-up task outside the current "
    "request (a nearby bug, missing tests, stale docs). Not for the current steps nor "
    "to ask permission; at most 1-2 per turn. If accepted, the prompt starts a new "
    "conversation: make it self-contained (what, where, how to check it is done). "
    "kind=plan_step: after an approved plan, one card per independent step (up to 4) "
    "that the user may start in parallel; do the remaining step here. context and "
    "isolation say how the child would open (see session.start)."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "prompt"],
    "properties": {
        "title": {"type": "string", "minLength": 3, "maxLength": 80},
        "prompt": {"type": "string", "minLength": 10, "maxLength": 2000},
        "rationale": {"type": "string", "maxLength": 200},
        "kind": {"type": "string", "enum": ["note", "plan_step"]},
        "done_when": {"type": "string", "maxLength": 500},
        "context": {"type": "string", "enum": ["brief", "fork"]},
        "isolation": {"type": "string", "enum": ["auto", "worktree", "same"]},
    },
}

_CODES = {
    "LIMIT": ToolErrorCode.RESOURCE_EXHAUSTED,
    "DUPLICATE": ToolErrorCode.ALREADY_EXISTS,
    "NOT_FOUND": ToolErrorCode.NOT_FOUND,
    "INVALID": ToolErrorCode.INVALID_ARGUMENT,
}


def _suggest(arguments: dict, ctx: ToolContext) -> ToolResult:
    service = getattr(ctx, "followups", None)
    if service is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "suggestions are not available here")
    turn_state = getattr(ctx, "turn_state", None)
    provenance = {
        "external_content": bool(getattr(turn_state, "external_content", False)),
        "sources": list(getattr(turn_state, "sources", []) or [])[:5],
    }
    try:
        suggestion, superseded = service.suggest(
            ctx.session_id,
            title=(arguments or {}).get("title"),
            prompt=(arguments or {}).get("prompt"),
            rationale=(arguments or {}).get("rationale", ""),
            provenance=provenance,
            kind=str((arguments or {}).get("kind") or "note"),
            spawn={
                key: (arguments or {}).get(key)
                for key in ("context", "isolation", "done_when")
                if (arguments or {}).get(key)
            },
        )
    except FollowupError as exc:
        return _fail(_CODES.get(exc.code, ToolErrorCode.INVALID_ARGUMENT), str(exc))
    sink = getattr(ctx, "activity_sink", None)
    if callable(sink):
        with contextlib.suppress(Exception):  # presentation never fails the note
            sink("followup.suggested", {"suggestion": suggestion.as_dict()})
            for old in superseded:
                sink(
                    "followup.resolved",
                    {"suggestion_id": old.id, "status": old.status, "suggestion": old.as_dict()},
                )
    return ToolResult(ok=True, data={"suggestion_id": suggestion.id, "status": "pending"})


def _fail(code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(ok=False, error=ToolErrorInfo(code=code, message=message, retryable=False))


def followup_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="followup.suggest",
            description=DESCRIPTION,
            input_schema=INPUT_SCHEMA,
            capabilities=("state.mutate",),
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=False,
            handler=_suggest,
            classify=lambda _input: ClassifiedAction("state.mutate"),
            namespace="followup",
            always_loaded=True,
        )
    ]
