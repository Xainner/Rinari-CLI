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
    "Leave the user a short note suggesting a worthwhile follow-up task you noticed "
    "while working: a nearby bug, missing tests, stale docs, a risky pattern worth "
    "fixing. Only for work outside the current request (the steps of the current task "
    "go in checklist.update), never to ask permission and never for something you are "
    "about to do anyway. At most one or two per turn, only when really useful. The note "
    "does not start anything: if the user accepts it, the prompt opens a new "
    "conversation, so write it self-contained: what to do, which files or areas, and "
    "how to know it is done."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "prompt"],
    "properties": {
        "title": {
            "type": "string",
            "minLength": 3,
            "maxLength": 80,
            "description": "A few words, e.g. 'Add tests for the parser'.",
        },
        "prompt": {
            "type": "string",
            "minLength": 10,
            "maxLength": 2000,
            "description": "The self-contained instruction that starts the new conversation.",
        },
        "rationale": {
            "type": "string",
            "maxLength": 200,
            "description": "Why it is worth doing (one sentence, optional).",
        },
    },
}

_CODES = {
    "LIMIT": ToolErrorCode.RESOURCE_EXHAUSTED,
    "DUPLICATE": ToolErrorCode.ALREADY_EXISTS,
    "NOT_FOUND": ToolErrorCode.NOT_FOUND,
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
