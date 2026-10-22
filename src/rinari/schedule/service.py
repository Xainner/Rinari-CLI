"""Scheduled tasks: what they are and what they may do.

A task is created by the owner (from the desktop form, the CLI, or by
confirming a `schedule.propose` card): the model never creates one. Creating
it is also approving what it needs — its `grants` are seeded as session grants
of every run, so a run does not stop to ask for what the owner already allowed.
Anything outside them still asks, and the run shows as `needs_you`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rinari.schedule.rules import (
    Schedule,
    ScheduleError,
    describe,
    next_run,
    parse_schedule,
    resolve_after,
)

KINDS = ("agent", "reminder", "command", "tool")
# A «command» task (plan 0.4.0 #50) runs without the model: zero tokens.
# A «tool» task (Rinari Agency, permanent rules) calls a connector tool with
# fixed arguments, also without the model, and drops the result somewhere.
TOOL_ON_RESULT = ("nothing", "notify", "agent", "deliver")
MAX_TOOL_ARGUMENTS = 8000
MAX_PER_DAY = 1000
#: In a delivery's arguments, replaced by the text the first tool returned.
RESULT_PLACEHOLDER = "{result}"
SHELLS = ("default", "cmd", "powershell", "bash")
ON_RESULT = ("nothing", "notify_failure", "agent")
MAX_COMMAND = 4000
DEFAULT_COMMAND_TIMEOUT_S = 600
MAX_COMMAND_TIMEOUT_S = 3600
MODES = ("plan", "build", "review")
# None: the project's default permission mode (plan permisos v4 P-5).
PERMISSION_PROFILES = ("read-only", "workspace", "full-access")
MAX_NAME = 80
MAX_PROMPT = 8000
MAX_GRANTS = 50
MAX_SKILLS = 10
RUN_STATUSES = (
    "queued",
    "running",
    "needs_you",
    "completed",
    "failed",
    "cancelled",
    "blocked",
    "skipped",
)
#: Where an agent task runs: a fresh session each time, or the conversation it
#: was asked in, as one more message with its history («Repetir aquí»).
TARGETS = ("new_session", "this_conversation")


class ScheduledTaskError(ValueError):
    """Invalid input for a task; the message is safe to show."""


def _command(raw: Any) -> dict[str, Any]:
    """What a «command» task runs, and what happens with its output (#50)."""
    if not isinstance(raw, dict):
        raise ScheduledTaskError("A command task needs its command.")
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND:
        raise ScheduledTaskError(f"The command must be 1..{MAX_COMMAND} characters.")
    shell = raw.get("shell") or "default"
    if shell not in SHELLS:
        raise ScheduledTaskError(f"Shell must be one of {', '.join(SHELLS)}.")
    cwd = raw.get("cwd") or None
    if cwd is not None and (not isinstance(cwd, str) or len(cwd) > 1000):
        raise ScheduledTaskError("The folder must be a path.")
    timeout = raw.get("timeout_s", DEFAULT_COMMAND_TIMEOUT_S)
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= MAX_COMMAND_TIMEOUT_S
    ):
        raise ScheduledTaskError(f"The time limit must be 1..{MAX_COMMAND_TIMEOUT_S} seconds.")
    env = raw.get("env") or {}
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise ScheduledTaskError("Environment variables must be text.")
    on_result = raw.get("on_result") or "notify_failure"
    if on_result not in ON_RESULT:
        raise ScheduledTaskError(f"on_result must be one of {', '.join(ON_RESULT)}.")
    return {
        "command": command.strip(),
        "shell": shell,
        "cwd": cwd,
        "timeout_s": timeout,
        "env": dict(env),
        "on_result": on_result,
    }


def _tool_call(raw: Any, what: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ScheduledTaskError(f"{what} needs its server and tool.")
    server = raw.get("server")
    tool = raw.get("tool")
    if not isinstance(server, str) or not server.strip() or len(server) > 200:
        raise ScheduledTaskError(f"{what} needs its connector (server).")
    if not isinstance(tool, str) or not tool.strip() or len(tool) > 256:
        raise ScheduledTaskError(f"{what} needs its tool.")
    arguments = raw.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ScheduledTaskError(f"{what}: arguments must be an object.")
    try:
        size = len(json.dumps(arguments))
    except (TypeError, ValueError):
        raise ScheduledTaskError(f"{what}: arguments must be plain JSON.") from None
    if size > MAX_TOOL_ARGUMENTS:
        raise ScheduledTaskError(
            f"{what}: arguments must be under {MAX_TOOL_ARGUMENTS} characters."
        )
    return {"server": server.strip(), "tool": tool.strip(), "arguments": arguments}


def _tool(raw: Any) -> dict[str, Any]:
    """What a «tool» task calls and where the result goes (Rinari Agency).

    The arguments are fixed when the owner creates the rule: an exact
    condition (a sender, a subject) lives in them, so nothing the tool
    returns can change what is called or where it goes.
    """
    spec = _tool_call(raw, "A tool task")
    on_result = raw.get("on_result") or "notify"
    if on_result not in TOOL_ON_RESULT:
        raise ScheduledTaskError(f"on_result must be one of {', '.join(TOOL_ON_RESULT)}.")
    spec["on_result"] = on_result
    if on_result == "deliver":
        spec["deliver"] = _tool_call(raw.get("deliver"), "A delivery")
    max_per_day = raw.get("max_per_day")
    if max_per_day is not None and (
        isinstance(max_per_day, bool)
        or not isinstance(max_per_day, int)
        or not 1 <= max_per_day <= MAX_PER_DAY
    ):
        raise ScheduledTaskError(f"max_per_day must be 1..{MAX_PER_DAY}.")
    spec["max_per_day"] = max_per_day
    contains = raw.get("only_if_contains")
    if contains is not None and (
        not isinstance(contains, str) or not contains.strip() or len(contains) > 200
    ):
        raise ScheduledTaskError("only_if_contains must be text of 1..200 characters.")
    # An exact condition on the result (CI: «failure»), checked without the model.
    spec["only_if_contains"] = contains.strip() if contains else None
    return spec


def _watch(raw: Any) -> dict[str, Any] | None:
    """A folder a rule watches: it runs only when the folder changed."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ScheduledTaskError("watch must be {path}.")
    path = raw.get("path")
    if not isinstance(path, str) or not path.strip() or len(path) > 1000:
        raise ScheduledTaskError("A watched folder needs its path.")
    folder = Path(path.strip()).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise ScheduledTaskError(f"The watched folder must be an existing folder: {path}")
    watch = {"path": str(folder)}
    # Kept while the folder stays the same: a new folder starts from scratch.
    fingerprint = raw.get("fingerprint")
    if isinstance(fingerprint, str) and raw.get("path") == str(folder):
        watch["fingerprint"] = fingerprint
    return watch


def _grant(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ScheduledTaskError("Each grant must be {capability, target?}.")
    capability = raw.get("capability")
    target = raw.get("target")
    if not isinstance(capability, str) or not capability.strip() or len(capability) > 120:
        raise ScheduledTaskError("A grant needs a capability.")
    if target is not None and (not isinstance(target, str) or len(target) > 1000):
        raise ScheduledTaskError("A grant target must be text.")
    return {"capability": capability.strip(), "target": target or None}


class ScheduleService:
    def __init__(
        self,
        repo: Any,
        clock: Any,
        ids: Any,
        *,
        project_exists: Callable[[str], bool] = lambda project_id: True,
        model_exists: Callable[[str], bool] = lambda model: True,
        skill_exists: Callable[[str], bool] = lambda name: True,
        agent_exists: Callable[[str], bool] = lambda agent_id: True,
        session_exists: Callable[[str], bool] = lambda session_id: True,
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._ids = ids
        self._project_exists = project_exists
        self._model_exists = model_exists
        self._skill_exists = skill_exists
        self._agent_exists = agent_exists
        self._session_exists = session_exists
        # Set by the Engine server: a validated proposal becomes the
        # `schedule.proposed` event the desktop shows as a card.
        self.on_proposed: Callable[[dict[str, Any]], None] | None = None
        # Set by the Engine server: a task a model changed or removed with its
        # tools (`schedule.changed`, so the desktop's lists follow).
        self.on_changed: Callable[[str, str], None] | None = None
        # Rinari Agency: checks where an agent's rule delivers (`deliver_to`)
        # and returns it with ids; raises ValueError with a safe message.
        self.check_delivery: Callable[[str, Any], dict[str, Any]] | None = None

    # -- validation --------------------------------------------------------

    def _normalize(
        self, raw: dict[str, Any], current: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        merged = {**(current or {}), **raw}
        name = merged.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME:
            raise ScheduledTaskError(f"The name must be 1..{MAX_NAME} characters.")
        kind = merged.get("kind", "agent")
        if kind not in KINDS:
            raise ScheduledTaskError("Kind must be agent, reminder, command or tool.")
        try:
            # Relative to now only when the schedule itself is being set.
            if "schedule" in raw:
                merged["schedule"] = resolve_after(raw["schedule"], self._clock.now())
            schedule = parse_schedule(merged.get("schedule"))
        except ScheduleError as exc:
            raise ScheduledTaskError(str(exc)) from None
        prompt = merged.get("prompt") or ""
        if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT:
            raise ScheduledTaskError(f"The prompt must be text up to {MAX_PROMPT} characters.")
        command = _command(merged.get("command")) if kind == "command" else None
        tool = _tool(merged.get("tool")) if kind == "tool" else None
        watch = _watch(merged.get("watch"))
        if watch is not None and kind == "reminder":
            raise ScheduledTaskError("A reminder cannot watch a folder.")
        # A command or tool task needs no prompt; with on_result=agent the
        # prompt is what to ask about the output.
        if not prompt.strip() and kind not in ("command", "tool"):
            raise ScheduledTaskError("Say what to do (agent) or what to remind (reminder).")
        project_id = merged.get("project_id") or None
        if project_id is not None and (
            not isinstance(project_id, str) or not self._project_exists(project_id)
        ):
            raise ScheduledTaskError(f"Unknown project: {project_id}")
        mode = merged.get("mode") or "build"
        if mode not in MODES:
            raise ScheduledTaskError("Mode must be plan, build or review.")
        permission_profile = merged.get("permission_profile") or None
        if permission_profile is not None and permission_profile not in PERMISSION_PROFILES:
            raise ScheduledTaskError("Permission mode must be read-only, workspace or full-access.")
        model = merged.get("model") or None
        if model is not None and (not isinstance(model, str) or not self._model_exists(model)):
            raise ScheduledTaskError(f"Unknown model: {model}")
        skills = merged.get("skills") or []
        if not isinstance(skills, list) or len(skills) > MAX_SKILLS:
            raise ScheduledTaskError(f"Skills must be a list of up to {MAX_SKILLS} names.")
        for skill in skills:
            if not isinstance(skill, str) or not self._skill_exists(skill):
                raise ScheduledTaskError(f"Unknown skill: {skill}")
        grants_raw = merged.get("grants") or []
        if not isinstance(grants_raw, list) or len(grants_raw) > MAX_GRANTS:
            raise ScheduledTaskError(f"Grants must be a list of up to {MAX_GRANTS}.")
        grants: list[dict[str, Any]] = []
        for grant in map(_grant, grants_raw):
            if grant not in grants:
                grants.append(grant)
        agent_id = merged.get("agent_id") or None
        if agent_id is not None and (
            not isinstance(agent_id, str) or not self._agent_exists(agent_id)
        ):
            raise ScheduledTaskError(f"Unknown agent: {agent_id}")
        enabled = merged.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ScheduledTaskError("'enabled' must be a boolean.")
        target = merged.get("target") or "new_session"
        if target not in TARGETS:
            raise ScheduledTaskError("Target must be new_session or this_conversation.")
        session_id = merged.get("session_id") or None
        if target == "this_conversation":
            if kind != "agent":
                raise ScheduledTaskError("Only an agent task can run in its conversation.")
            if not isinstance(session_id, str) or not self._session_exists(session_id):
                raise ScheduledTaskError(f"Unknown conversation: {session_id}")
            # It runs with the conversation's own model, mode and permissions,
            # and asks for what it needs: a grant would outlive the run there.
            if grants:
                raise ScheduledTaskError(
                    "A task that runs in its conversation carries no grants: each run asks."
                )
            model, skills, permission_profile = None, [], None
        else:
            session_id = None
        deliver_to = self._delivery(agent_id, merged.get("deliver_to"))
        return {
            "name": name.strip(),
            "kind": kind,
            "schedule": schedule.to_dict(),
            "prompt": prompt.strip(),
            "project_id": project_id,
            "mode": mode,
            "permission_profile": permission_profile,
            "model": model,
            "skills": list(dict.fromkeys(skills)),
            "grants": grants,
            "command": command,
            "tool": tool,
            "watch": watch,
            "agent_id": agent_id,
            "target": target,
            "session_id": session_id,
            "deliver_to": deliver_to,
            "enabled": enabled,
        }

    def _delivery(self, agent_id: str | None, raw: Any) -> dict[str, Any] | None:
        """Where an agent's rule also delivers its result: its team's office or a
        teammate (Rinari Agency). Only an agent's rule has an office."""
        if raw is None or raw == {}:
            return None
        if agent_id is None:
            raise ScheduledTaskError("Only a Rinari Agency agent's rule delivers to an office.")
        if self.check_delivery is None:
            raise ScheduledTaskError("Delivering to an office needs the Agency.")
        try:
            return self.check_delivery(agent_id, raw)
        except ValueError as exc:
            raise ScheduledTaskError(str(exc)) from None

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        """A task as `create` would store it, without storing it (proposals)."""
        return self._normalize(raw)

    def propose(
        self, raw: dict[str, Any], *, session_id: str = "", create: bool = False
    ) -> dict[str, Any]:
        """Validate a model's draft and announce it.

        With `create` (the owner asked in their own turn) the task is created
        at once; otherwise nothing is stored and the owner confirms it. Either
        way it carries no grants: permissions are only the owner's to give,
        and anything a run needs is asked for when it runs."""
        draft = self._normalize(raw)
        draft["grants"] = []
        result: dict[str, Any] = {
            "proposal": draft,
            "description": describe(parse_schedule(draft["schedule"])),
            "session_id": session_id,
            "created": False,
        }
        if create:
            result["task"] = self.create(draft)
            result["created"] = True
        if self.on_proposed is not None:
            self.on_proposed(result)
        return result

    # -- tasks -------------------------------------------------------------

    def view(self, task: dict[str, Any]) -> dict[str, Any]:
        schedule = parse_schedule(task["schedule"])
        return {
            **task,
            "description": describe(schedule),
            "last_run": self._repo.last_run(task["id"]),
        }

    def list(self) -> list[dict[str, Any]]:
        return [self.view(task) for task in self._repo.list()]

    def get(self, task_id: str) -> dict[str, Any]:
        task = self._repo.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def create(self, raw: dict[str, Any]) -> dict[str, Any]:
        task = self._normalize(raw)
        now = self._clock.now()
        task_id = self._ids.new("sch")
        task.update(created_at=now, updated_at=now)
        task["next_run_at"] = self._first_run(task, now)
        return self.view(self._repo.save(task_id, task))

    def update(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.get(task_id)
        task = self._normalize(patch, current)
        now = self._clock.now()
        task.update(created_at=current["created_at"], updated_at=now)
        rescheduled = task["schedule"] != current["schedule"] or (
            task["enabled"] and not current["enabled"]
        )
        # A new schedule, or switching a task back on, counts from now: runs
        # missed while it was off are not caught up.
        task["next_run_at"] = self._first_run(task, now) if rescheduled else current["next_run_at"]
        if "enabled" not in patch and not task["enabled"]:
            # Still off, and not by the owner's hand now: who paused it stays known
            # («Detener a todos» switches back on only what it switched off).
            task["paused_by"] = current.get("paused_by")
        return self.view(self._repo.save(task_id, task))

    def delete(self, task_id: str) -> bool:
        return self._repo.delete(task_id)

    def add_grant(self, task_id: str, capability: str, target: str | None) -> dict[str, Any]:
        task = self.get(task_id)
        grant = _grant({"capability": capability, "target": target})
        grants = list(task["grants"])
        if grant not in grants:
            grants.append(grant)
        return self.update(task_id, {"grants": grants})

    def _first_run(self, task: dict[str, Any], now: float) -> float | None:
        return next_run(self._schedule(task), now)

    @staticmethod
    def _schedule(task: dict[str, Any]) -> Schedule:
        return parse_schedule(task["schedule"])

    def advance(self, task: dict[str, Any], after: float) -> dict[str, Any]:
        """Move a task past a run (done, skipped or started by hand)."""
        upcoming = next_run(self._schedule(task), after)
        fields = {**task, "next_run_at": upcoming, "updated_at": self._clock.now()}
        if upcoming is None:
            # A one-time task that ran is kept (with its history), switched off.
            fields["enabled"] = False
        fields.pop("description", None)
        fields.pop("last_run", None)
        return self._repo.save(task["id"], fields)
