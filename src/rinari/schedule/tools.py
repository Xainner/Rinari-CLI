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

    def mine(task: dict[str, Any]) -> bool:
        # An agent's tools reach only its own rules; Rinari's, the owner's tasks
        # (never an agent's rule).
        return task.get("agent_id") == host.agent_id if host.agent_id else not task.get("agent_id")

    def fail(code, message: str):
        return ToolResult(ok=False, error=ToolErrorInfo(code, message), origin="schedule")

    def find(ref: Any) -> tuple[dict[str, Any] | None, str | None]:
        """One of mine by id, or by its exact name (any case)."""
        if not isinstance(ref, str) or not ref.strip():
            return None, "task must be a task's id or name (see schedule.list)"
        tasks = [task for task in host.service.list() if mine(task)]
        for task in tasks:
            if task["id"] == ref:
                return task, None
        named = [task for task in tasks if (task.get("name") or "").lower() == ref.strip().lower()]
        if len(named) == 1:
            return named[0], None
        if named:
            ids = ", ".join(task["id"] for task in named)
            return None, f"Several tasks are named {ref}: use one id ({ids})."
        return None, f"No task of yours named {ref} (see schedule.list)."

    def changed(task_id: str, change: str) -> None:
        if getattr(host.service, "on_changed", None) is not None:
            host.service.on_changed(task_id, change)

    def view(task: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": task["id"],
            "name": task["name"],
            "kind": task.get("kind"),
            "when": task.get("description"),
            "enabled": bool(task.get("enabled")),
            "next_run_local": _local(task.get("next_run_at")) if task.get("enabled") else None,
            "here": task.get("target") == "this_conversation",
            "notify": task.get("notify") or "all",
            "prompt": (task.get("prompt") or "")[:300],
        }

    def list_mine(arguments, ctx):
        if host.service is None:
            return fail(ToolErrorCode.DEPENDENCY_ERROR, "scheduling is not available")
        tasks = [view(task) for task in host.service.list() if mine(task)]
        return ToolResult(ok=True, data={"tasks": tasks}, origin="schedule")

    def update(arguments, ctx):
        if host.service is None:
            return fail(ToolErrorCode.DEPENDENCY_ERROR, "scheduling is not available")
        args = dict(arguments or {})
        task, problem = find(args.pop("task", None))
        if task is None:
            return fail(ToolErrorCode.NOT_FOUND, problem or "not found")
        patch = {key: args[key] for key in ("name", "schedule", "prompt", "notify") if key in args}
        if "enabled" in args:
            if not isinstance(args["enabled"], bool):
                return fail(ToolErrorCode.INVALID_ARGUMENT, "enabled must be true or false")
            if args["enabled"] and task.get("paused_by"):
                # Paused by the owner's stop: only the owner switches it back on.
                return fail(
                    ToolErrorCode.INVALID_ARGUMENT,
                    "The owner paused it with a stop; only the owner resumes it.",
                )
            patch["enabled"] = args["enabled"]
        if not patch:
            return fail(
                ToolErrorCode.INVALID_ARGUMENT,
                "Say what changes: schedule, prompt, name or enabled.",
            )
        try:
            saved = host.service.update(task["id"], patch)
        except ScheduledTaskError as exc:
            return fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))
        changed(saved["id"], "updated")
        return ToolResult(ok=True, data={"status": "updated", **view(saved)}, origin="schedule")

    def cancel(arguments, ctx):
        if host.service is None:
            return fail(ToolErrorCode.DEPENDENCY_ERROR, "scheduling is not available")
        task, problem = find((arguments or {}).get("task"))
        if task is None:
            return fail(ToolErrorCode.NOT_FOUND, problem or "not found")
        host.service.delete(task["id"])
        changed(task["id"], "deleted")
        return ToolResult(
            ok=True,
            data={"status": "cancelled", "id": task["id"], "name": task["name"]},
            origin="schedule",
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
        # The same name again, in the same place, is the same task changed
        # (owner, 22-oct: «cada minuto» left the 30-second one running too).
        same = [
            task
            for task in host.service.list()
            if mine(task)
            and (task.get("name") or "").lower() == str(args.get("name") or "").strip().lower()
            and task.get("session_id") == args.get("session_id")
        ]
        if len(same) == 1:
            try:
                saved = host.service.update(same[0]["id"], args)
            except ScheduledTaskError as exc:
                return fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))
            changed(saved["id"], "updated")
            return ToolResult(
                ok=True,
                data={
                    "status": "updated",
                    "task_id": saved["id"],
                    "description": saved.get("description") or "",
                    "next_run_at": saved.get("next_run_at"),
                    "next_run_local": _local(saved.get("next_run_at")),
                    "note": "A task of yours had this name here: it was changed, not duplicated.",
                },
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
                "mentions that teammate in your shared office and wakes only them. "
                "The same name again in the same place changes that task; to see, change or "
                "stop what you scheduled use schedule.list, schedule.update and schedule.cancel."
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
                    "notify": {
                        "type": "string",
                        "enum": ["all", "problems", "none"],
                        "description": "How much its runs notify the owner: every run (all, "
                        "the default), only a failure or one that needs them (problems), or "
                        "nothing that pops up (none). Set it when the owner asks.",
                    },
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
            # Always in the list: a model that had to search for it answered «I can't
            # check every minute» and held its turn with a wait instead (22-oct).
            always_loaded=True,
            classify=lambda _i: ClassifiedAction("state.write"),
            handler=propose,
        ),
        ToolDefinition(
            name="schedule.list",
            description=(
                "What you scheduled (your rules, as an Agency agent): each task's id, name, "
                "when it runs, whether it is on and its next run in local time."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            capabilities=("state.read",),
            side_effects="none",
            always_loaded=False,
            classify=lambda _i: ClassifiedAction("state.read"),
            handler=list_mine,
        ),
        ToolDefinition(
            name="schedule.update",
            description=(
                "Change one of your scheduled tasks (task: its id or name): its schedule (same "
                "shapes as schedule.propose), prompt or name, or switch it off (enabled: false) "
                "and back on. It keeps its id and history."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "name": {"type": "string", "maxLength": 80},
                    "schedule": {"type": "object"},
                    "prompt": {"type": "string", "maxLength": 8000},
                    "enabled": {"type": "boolean"},
                    "notify": {"type": "string", "enum": ["all", "problems", "none"]},
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            capabilities=("state.write",),
            side_effects="local_reversible",
            always_loaded=False,
            classify=lambda _i: ClassifiedAction("state.write"),
            handler=update,
        ),
        ToolDefinition(
            name="schedule.cancel",
            description=(
                "Remove one of your scheduled tasks (task: its id or name): it never runs again."
            ),
            input_schema={
                "type": "object",
                "properties": {"task": {"type": "string"}},
                "required": ["task"],
                "additionalProperties": False,
            },
            capabilities=("state.write",),
            side_effects="local_reversible",
            always_loaded=False,
            classify=lambda _i: ClassifiedAction("state.write"),
            handler=cancel,
        ),
    ]


def _local(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M") if epoch else None


__all__ = ["ScheduleToolHost", "schedule_tools"]
