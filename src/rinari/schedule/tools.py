"""`schedule.propose`: the model drafts a scheduled task, the owner creates it.

The tool validates the draft exactly as `schedule.create` would and returns
it as a proposal; nothing is stored. The desktop shows it as a card and only
the owner's confirmation (`schedule.create`) makes it a task — the model never
schedules work on its own, and never grants itself permissions for later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class ScheduleToolHost:
    service: Any
    #: The session's project: a proposal made in a project runs there.
    project_id: str | None = None
    #: Rinari Agency: the agent whose conversation this is. Its proposals are
    #: its permanent rules, and a rule is always confirmed by the owner.
    agent_id: str | None = None


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
        # «Repetir aquí»: this conversation, with its history, instead of a
        # fresh session for every run. Only the conversation it was asked in.
        args.pop("target", None)
        args.pop("session_id", None)
        if args.pop("in_this_conversation", False):
            args["target"] = "this_conversation"
            args["session_id"] = getattr(ctx, "session_id", "") or None
        # Grants are the owner's to give, in the confirmation card.
        args.pop("grants", None)
        args.pop("enabled", None)
        # Scheduling goes by permissions like any other action: the tool is a
        # `state.write`, which read-only refuses and the writing modes allow,
        # and each run asks for what it needs (no grants). The one exception
        # is structural: a run of a scheduled task does not schedule more,
        # or a task every few minutes would multiply.
        if (getattr(ctx, "origin_kind", "user") or "user") == "schedule":
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(
                    ToolErrorCode.INVALID_ARGUMENT,
                    "A scheduled run cannot schedule more tasks.",
                ),
                origin="schedule",
            )
        if host.agent_id:
            args["agent_id"] = host.agent_id
        elif args.get("kind") == "tool":
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(
                    ToolErrorCode.INVALID_ARGUMENT,
                    "Only a Rinari Agency agent's rules call a connector without the model.",
                ),
                origin="schedule",
            )
        try:
            proposed = host.service.propose(
                args, session_id=getattr(ctx, "session_id", "") or "", create=True
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
                    # The machine's local time: what to tell the owner.
                    "next_run_local": _local(proposed["task"]["next_run_at"]),
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
                "repeatedly (a reminder, or a prompt Rinari runs at that time). It is created "
                "at once, as permissions allow. It never carries permissions: a run asks for "
                "what it needs. "
                "schedule is local time: {kind: once, at: 'YYYY-MM-DDTHH:MM'} | "
                "{kind: once, after: {minutes, hours, days}} (counted from now, no need to "
                "know the time) | {kind: interval, minutes} or {kind: interval, seconds} "
                "(from 5 seconds; a run never overlaps the previous one, it is skipped) | "
                "{kind: daily, time: 'HH:MM'} | {kind: weekly, days: [0-6, 0=Monday], "
                "time: 'HH:MM'} | {kind: cron, expr: 'minute hour day month weekday'}. For kind "
                "'agent' each run starts in a fresh session, so the prompt must stand alone, "
                "unless in_this_conversation is true: then it runs in this conversation as a "
                "new message, with its history. In a Rinari "
                "Agency agent's conversation this creates one of its permanent rules, and "
                "kind 'tool' calls one of its connectors with "
                "fixed arguments and no model: tool = {server, tool, arguments, on_result: "
                "nothing | notify | agent | deliver, deliver: {server, tool, arguments with "
                "'{result}'}, max_per_day, only_if_contains: exact text the result must "
                "contain (e.g. 'failure' for a CI)}. watch = {path} runs it only when that "
                "folder changed since the last check. deliver_to (an agent's rule only) also "
                "posts each run's result: {kind: office, team: '<team name>'} writes it in the "
                "office of a team you work in, as you (e.g. a one-line reminder every 30 "
                "minutes: kind reminder, schedule {kind: interval, minutes: 30}, prompt the "
                "line, deliver_to {kind: office, team}); {kind: agent, agent: '<teammate>'} "
                "mentions that teammate in your shared office and wakes only them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "maxLength": 80},
                    "kind": {"type": "string", "enum": ["agent", "reminder", "tool"]},
                    "tool": {"type": "object"},
                    "watch": {"type": "object"},
                    "schedule": {"type": "object"},
                    "prompt": {"type": "string", "maxLength": 8000},
                    "mode": {"type": "string", "enum": ["plan", "build", "review"]},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "in_this_project": {"type": "boolean"},
                    "in_this_conversation": {"type": "boolean"},
                    "deliver_to": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["office", "agent"]},
                            "team": {"type": "string", "description": "A team's name."},
                            "agent": {"type": "string", "description": "A teammate's name."},
                        },
                        "required": ["kind"],
                        "additionalProperties": False,
                    },
                },
                "required": ["name", "kind", "schedule"],
                "additionalProperties": False,
            },
            capabilities=("state.write",),
            side_effects="local_reversible",
            always_loaded=False,
            classify=lambda _i: ClassifiedAction("state.write"),
            handler=propose,
        )
    ]


def _local(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M") if epoch else None


__all__ = ["ScheduleToolHost", "schedule_tools"]
