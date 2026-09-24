"""`schedule.propose`: the model drafts a scheduled task, the owner creates it.

The tool validates the draft exactly as `schedule.create` would and returns
it as a proposal; nothing is stored. The desktop shows it as a card and only
the owner's confirmation (`schedule.create`) makes it a task — the model never
schedules work on its own, and never grants itself permissions for later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ScheduleToolHost:
    service: Any
    #: The session's project: a proposal made in a project runs there.
    project_id: str | None = None


def schedule_tools(host: ScheduleToolHost):
    from rinari.schedule.service import ScheduledTaskError
    from rinari.tools.definition import (
        ClassifiedAction,
        ToolDefinition,
        ToolErrorCode,
        ToolErrorInfo,
        ToolResult,
    )

    def propose(arguments, ctx):
        args = dict(arguments or {})
        if host.service is None:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.DEPENDENCY_ERROR, "scheduling is not available"),
                origin="schedule",
            )
        if args.pop("in_this_project", True) and host.project_id:
            args.setdefault("project_id", host.project_id)
        # Grants are the owner's to give, in the confirmation card.
        args.pop("grants", None)
        args.pop("enabled", None)
        try:
            proposed = host.service.propose(args, session_id=getattr(ctx, "session_id", "") or "")
        except ScheduledTaskError as exc:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.INVALID_ARGUMENT, str(exc)),
                origin="schedule",
            )
        return ToolResult(
            ok=True,
            data={
                "status": "proposed",
                "proposal": proposed["proposal"],
                "description": proposed["description"],
                "note": "Nothing is scheduled until the owner confirms this proposal.",
            },
            origin="schedule",
        )

    return [
        ToolDefinition(
            name="schedule.propose",
            description=(
                "Propose a scheduled task when the owner asks for something to happen later "
                "or repeatedly (a reminder, or a prompt Rinari runs at that time). Returns a "
                "proposal the owner confirms in the app; nothing is scheduled until then. "
                "schedule is local time: {kind: once, at: 'YYYY-MM-DDTHH:MM'} | "
                "{kind: interval, minutes} | {kind: daily, time: 'HH:MM'} | "
                "{kind: weekly, days: [0-6, 0=Monday], time: 'HH:MM'}. For kind 'agent' the "
                "prompt must stand alone: the run starts in a fresh session."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "maxLength": 80},
                    "kind": {"type": "string", "enum": ["agent", "reminder"]},
                    "schedule": {"type": "object"},
                    "prompt": {"type": "string", "maxLength": 8000},
                    "mode": {"type": "string", "enum": ["plan", "build", "review"]},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "in_this_project": {"type": "boolean"},
                },
                "required": ["name", "kind", "schedule", "prompt"],
                "additionalProperties": False,
            },
            capabilities=("state.read",),
            always_loaded=False,
            classify=lambda _i: ClassifiedAction("state.read"),
            handler=propose,
        )
    ]


__all__ = ["ScheduleToolHost", "schedule_tools"]
