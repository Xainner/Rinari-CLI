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
        # The owner asking in their own turn is the confirmation. A turn that
        # came from another pane, another task or a channel only proposes.
        owner_asked = (getattr(ctx, "origin_kind", "user") or "user") == "user"
        try:
            proposed = host.service.propose(
                args, session_id=getattr(ctx, "session_id", "") or "", create=owner_asked
            )
        except ScheduledTaskError as exc:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.INVALID_ARGUMENT, str(exc)),
                origin="schedule",
            )
        return ToolResult(
            ok=True,
            data=(
                {
                    "status": "created",
                    "task_id": proposed["task"]["id"],
                    "description": proposed["description"],
                    "next_run_at": proposed["task"]["next_run_at"],
                    "note": (
                        "Scheduled. It has no permissions granted in advance: a run asks "
                        "for what it needs. The owner can undo it from the notice."
                    ),
                }
                if proposed["created"]
                else {
                    "status": "proposed",
                    "proposal": proposed["proposal"],
                    "description": proposed["description"],
                    "note": "Nothing is scheduled until the owner confirms this proposal.",
                }
            ),
            origin="schedule",
        )

    return [
        ToolDefinition(
            name="schedule.propose",
            description=(
                "Schedule a task when the owner asks for something to happen later or "
                "repeatedly (a reminder, or a prompt Rinari runs at that time). When the owner "
                "asked in this conversation it is created at once; a request that came from "
                "another pane or task is only proposed for the owner to confirm. It never "
                "carries permissions: a run asks for what it needs. "
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
            capabilities=("state.write",),
            side_effects="local_reversible",
            always_loaded=False,
            classify=lambda _i: ClassifiedAction("state.write"),
            handler=propose,
        )
    ]


__all__ = ["ScheduleToolHost", "schedule_tools"]
