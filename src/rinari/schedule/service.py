"""Scheduled tasks: what they are and what they may do.

A task is created by the owner (from the desktop form, the CLI, or by
confirming a `schedule.propose` card): the model never creates one. Creating
it is also approving what it needs — its `grants` are seeded as session grants
of every run, so a run does not stop to ask for what the owner already allowed.
Anything outside them still asks, and the run shows as `needs_you`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rinari.schedule.rules import Schedule, ScheduleError, describe, next_run, parse_schedule

KINDS = ("agent", "reminder")
MODES = ("plan", "build", "review")
MAX_NAME = 80
MAX_PROMPT = 8000
MAX_GRANTS = 50
MAX_SKILLS = 10
RUN_STATUSES = ("running", "needs_you", "completed", "failed", "cancelled", "blocked", "skipped")


class ScheduledTaskError(ValueError):
    """Invalid input for a task; the message is safe to show."""


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
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._ids = ids
        self._project_exists = project_exists
        self._model_exists = model_exists
        self._skill_exists = skill_exists
        # Set by the Engine server: a validated proposal becomes the
        # `schedule.proposed` event the desktop shows as a card.
        self.on_proposed: Callable[[dict[str, Any]], None] | None = None

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
            raise ScheduledTaskError("Kind must be agent or reminder.")
        try:
            schedule = parse_schedule(merged.get("schedule"))
        except ScheduleError as exc:
            raise ScheduledTaskError(str(exc)) from None
        prompt = merged.get("prompt") or ""
        if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT:
            raise ScheduledTaskError(f"The prompt must be text up to {MAX_PROMPT} characters.")
        if not prompt.strip():
            raise ScheduledTaskError("Say what to do (agent) or what to remind (reminder).")
        project_id = merged.get("project_id") or None
        if project_id is not None and (
            not isinstance(project_id, str) or not self._project_exists(project_id)
        ):
            raise ScheduledTaskError(f"Unknown project: {project_id}")
        mode = merged.get("mode") or "build"
        if mode not in MODES:
            raise ScheduledTaskError("Mode must be plan, build or review.")
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
        enabled = merged.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ScheduledTaskError("'enabled' must be a boolean.")
        return {
            "name": name.strip(),
            "kind": kind,
            "schedule": schedule.to_dict(),
            "prompt": prompt.strip(),
            "project_id": project_id,
            "mode": mode,
            "model": model,
            "skills": list(dict.fromkeys(skills)),
            "grants": grants,
            "enabled": enabled,
        }

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
