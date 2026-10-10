"""checklist.update: the live list of steps Rinari is carrying out.

The desktop shows it in a dock above the composer while the work happens;
the CLI prints it. It only exists when the model keeps it: simple questions
and small edits never get one.
"""

from __future__ import annotations

import contextlib
from typing import Any

from rinari.checklist import CHECKLIST_MAX_ITEMS, ITEM_STATUSES, ChecklistError
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
    "Live list of your steps for multi-step work (3+ steps, several files, or a task "
    "list from the user); never for simple questions or chats. Send the whole list "
    "each call, one item in_progress at a time, completed only when really done, "
    "blocked with blocked_reason. Keep it true when the plan changes; [] clears it."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "maxItems": CHECKLIST_MAX_ITEMS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "content", "status"],
                "properties": {
                    "id": {"type": "string", "maxLength": 32},
                    "content": {"type": "string", "maxLength": 200},
                    "status": {"enum": list(ITEM_STATUSES)},
                    "active_form": {"type": "string", "maxLength": 120},
                    "blocked_reason": {"type": "string", "maxLength": 200},
                },
            },
        },
        "explanation": {"type": "string", "maxLength": 300},
    },
}

_MARKS = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "blocked": "[!]"}


def _fail(code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(ok=False, error=ToolErrorInfo(code=code, message=message, retryable=False))


def _update(arguments: dict, ctx: ToolContext) -> ToolResult:
    service = getattr(ctx, "checklist", None)
    if service is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "the checklist is not available here")
    try:
        checklist, warnings = service.replace(
            ctx.session_id, (arguments or {}).get("items"), (arguments or {}).get("explanation")
        )
    except ChecklistError as exc:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))
    _announce(ctx, checklist)
    return ToolResult(
        ok=True,
        data={"revision": checklist.revision, "counts": checklist.counts(), "warnings": warnings},
    )


def _announce(ctx: ToolContext, checklist) -> None:
    """`checklist.updated` on the turn (desktop dock) or the printed list (CLI).

    Presentation only: a failing sink never fails the update.
    """
    sink = getattr(ctx, "activity_sink", None)
    if callable(sink):
        with contextlib.suppress(Exception):
            sink("checklist.updated", {"reason": "model", "checklist": checklist.as_dict()})
        return
    output = getattr(ctx, "output_sink", None)
    if callable(output):
        lines = [f"{_MARKS[item['status']]} {item['content']}" for item in checklist.items]
        with contextlib.suppress(Exception):
            output("checklist", "\n".join(lines or ["(checklist cleared)"]) + "\n")


def checklist_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="checklist.update",
            description=DESCRIPTION,
            input_schema=INPUT_SCHEMA,
            capabilities=("state.mutate",),
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=False,
            handler=_update,
            classify=lambda _input: ClassifiedAction("state.mutate"),
            namespace="checklist",
            always_loaded=True,
        )
    ]
