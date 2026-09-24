"""Scheduled tasks in the Engine: the clock thread, runs and their methods.

The Engine runs while the desktop app is open or in the tray; a task that came
due while it was closed runs when it comes back, within the catch-up window,
and is recorded as skipped after that.

A run of an `agent` task is an ordinary turn in a session of its own
("⏰ name · date"), with `origin = {"kind": "schedule", ...}`. The task's grants
are seeded as session grants of that session, so the run does not ask for
what the owner approved when creating it. Anything else asks as usual: the
run turns `needs_you`, the desktop is told (`schedule.run.needs_you`), and if
nobody answers before the approval expires the run ends `blocked`, with what
it needed as its reason — never a silent failure.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.engine_protocol.messages import event
from rinari.policy.approvals import ApprovalGrant, GrantScope
from rinari.schedule.rules import CATCH_UP_WINDOW_S
from rinari.schedule.service import ScheduledTaskError
from rinari.shared.errors import NotFoundError

if TYPE_CHECKING:
    from rinari.engine_protocol.server import EngineServer

logger = logging.getLogger(__name__)

TICK_S = 20.0
SUMMARY_CHARS = 600
_ACTIVE = ("running", "needs_you")
_TERMINAL = {
    "turn.completed": "completed",
    "turn.failed": "failed",
    "turn.cancelled": "cancelled",
    "turn.stopped": "cancelled",
}


class ScheduleRunner:
    def __init__(self, server: EngineServer) -> None:
        self._server = server
        self._services = server._services
        self._turns = server._turns
        self._clock = self._services.ctx.clock
        self._repo = self._services.ctx.schedule_repo
        self._lock = threading.RLock()
        # session_id -> run_id of runs whose turn is live in this process.
        self._live: dict[str, str] = {}
        # run_id -> what an expired approval wanted (the run ends blocked).
        self._blocked: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._recovered = False
        self._turns.add_observer(self._observe)

    @property
    def tasks(self):
        return self._services.schedules

    # -- clock ---------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="rinari-scheduler", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("scheduler tick failed")
            self._stop.wait(TICK_S)

    def tick(self, now: float | None = None) -> list[str]:
        """Start what is due. Returns the run ids it created (tests)."""
        now = self._clock.now() if now is None else now
        created: list[str] = []
        with self._lock:
            if not self._recovered:
                self._recover(now)
            for task in self._repo.due(now):
                due_at = task["next_run_at"]
                if now - due_at > CATCH_UP_WINDOW_S:
                    created.append(self._record_skipped(task, due_at, "missed"))
                elif self._has_active_run(task["id"]):
                    created.append(self._record_skipped(task, due_at, "previous_run_active"))
                else:
                    created.append(self._run(task, trigger="schedule", scheduled_for=due_at))
                self.tasks.advance(task, now)
        return created

    def _recover(self, now: float) -> None:
        # Runs left open by a previous Engine process died with it.
        self._recovered = True
        for run in self._repo.unfinished_runs():
            if run["session_id"] in self._live:
                continue
            self._repo.save_run(
                run["id"], status="failed", finished_at=now, reason="engine_restarted"
            )

    def _has_active_run(self, task_id: str) -> bool:
        return any(run["status"] in _ACTIVE for run in self._repo.runs(task_id, limit=5))

    def _record_skipped(self, task: dict[str, Any], due_at: float, reason: str) -> str:
        run_id = self._services.ctx.ids.new("run")
        self._repo.save_run(
            run_id,
            task_id=task["id"],
            status="skipped",
            trigger="schedule",
            scheduled_for=due_at,
            finished_at=self._clock.now(),
            reason=reason,
        )
        self._emit("schedule.run.completed", task, run_id, status="skipped", reason=reason)
        return run_id

    # -- runs ------------------------------------------------------------------

    def run_now(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._task(task_id)
            if self._has_active_run(task_id):
                raise EngineProtocolError(
                    "SCHEDULE_RUN_ACTIVE", "This task already has a run in progress."
                )
            run_id = self._run(task, trigger="manual", scheduled_for=None)
        return {"run": self._repo.run(run_id)}

    def _run(self, task: dict[str, Any], *, trigger: str, scheduled_for: float | None) -> str:
        run_id = self._services.ctx.ids.new("run")
        started = self._clock.now()
        self._repo.save_run(
            run_id,
            task_id=task["id"],
            status="running",
            trigger=trigger,
            scheduled_for=scheduled_for,
            started_at=started,
        )
        if task["kind"] == "reminder":
            self._finish(task, run_id, "completed", summary=task["prompt"])
            return run_id
        try:
            session_id, turn_id = self._start_agent_run(task, run_id, started)
        except Exception as exc:
            message = getattr(exc, "message", None) or str(exc)
            self._finish(task, run_id, "failed", reason=message[:500])
            return run_id
        self._repo.save_run(run_id, session_id=session_id, turn_id=turn_id)
        self._emit("schedule.run.started", task, run_id, session_id=session_id)
        return run_id

    def _start_agent_run(
        self, task: dict[str, Any], run_id: str, started: float
    ) -> tuple[str, str]:
        stamp = datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M")
        created = self._server._session_create(
            {
                "chat": task["project_id"] is None,
                **({"project_id": task["project_id"]} if task["project_id"] else {}),
                "title": f"⏰ {task['name']} · {stamp}",
                "mode": task["mode"],
            }
        )
        session_id = created["session"]["id"]
        if task["model"]:
            self._services.sessions.set_model(session_id, task["model"], None)
        # Approved when the task was created: the run does not ask again.
        grants = self._turns.peers.session_grants(session_id)
        for grant in task["grants"]:
            grants.append(
                ApprovalGrant(
                    capability=grant["capability"],
                    scope=GrantScope.SESSION,
                    target=grant.get("target"),
                    session_id=session_id,
                )
            )
        record = self._services.sessions.show(session_id)
        project = record.project_root_snapshot
        for skill in task["skills"]:
            self._services.skills.activate(skill, session_id, Path(project) if project else None)
        with self._lock:
            self._live[session_id] = run_id
        try:
            started_turn = self._turns.start_turn(
                session_id,
                task["prompt"],
                memory_origin="automation",
                origin={"kind": "schedule", "task_id": task["id"], "run_id": run_id},
            )
        except Exception:
            with self._lock:
                self._live.pop(session_id, None)
            raise
        return session_id, started_turn["turn_id"]

    def _finish(self, task: dict[str, Any], run_id: str, status: str, **fields: Any) -> None:
        self._repo.save_run(run_id, status=status, finished_at=self._clock.now(), **fields)
        run = self._repo.run(run_id) or {}
        self._emit(
            "schedule.run.completed",
            task,
            run_id,
            status=status,
            session_id=run.get("session_id"),
            summary=run.get("summary"),
            reason=run.get("reason"),
        )

    # -- following the turn ----------------------------------------------------

    def _observe(self, frame: dict[str, Any]) -> None:
        name = frame.get("event")
        payload = frame.get("payload") or {}
        session_id = payload.get("session_id")
        if not name or not session_id:
            return
        with self._lock:
            run_id = self._live.get(session_id)
        if run_id is None:
            return
        run = self._repo.run(run_id)
        task = self._repo.get(run["task_id"]) if run else None
        if run is None or task is None:
            return
        if name == "approval.requested":
            reason = _wanted(payload)
            self._repo.save_run(run_id, status="needs_you", reason=reason)
            self._emit(
                "schedule.run.needs_you",
                task,
                run_id,
                session_id=session_id,
                approval_id=payload.get("approval_id"),
                capability=payload.get("capability"),
                target=payload.get("target"),
                reason=reason,
            )
        elif name == "approval.resolved":
            if run["status"] == "needs_you":
                self._repo.save_run(run_id, status="running")
        elif name == "approval.expired":
            with self._lock:
                self._blocked[run_id] = run.get("reason") or "approval_expired"
        elif name in _TERMINAL:
            with self._lock:
                self._live.pop(session_id, None)
                blocked = self._blocked.pop(run_id, None)
            status = _TERMINAL[name]
            summary = None
            if name == "turn.completed":
                content = payload.get("content")
                summary = content[:SUMMARY_CHARS] if isinstance(content, str) else None
            elif name == "turn.failed":
                error = payload.get("error") or {}
                blocked = blocked or (error.get("message") if isinstance(error, dict) else None)
            if blocked and status == "completed":
                status = "blocked"
            self._finish(task, run_id, status, summary=summary, reason=blocked)

    def _emit(self, name: str, task: dict[str, Any], run_id: str, **fields: Any) -> None:
        self._turns.emit_external(
            event(
                name,
                {
                    "task_id": task["id"],
                    "run_id": run_id,
                    "name": task["name"],
                    "kind": task["kind"],
                    **{key: value for key, value in fields.items() if value is not None},
                },
            )
        )

    # -- methods -----------------------------------------------------------------

    def _task(self, task_id: Any) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'task_id' must be a non-empty string.")
        task = self._repo.get(task_id)
        if task is None:
            raise NotFoundError(f"Unknown scheduled task: {task_id}")
        return task

    def _changed(self, task_id: str, change: str) -> None:
        self._turns.emit_external(event("schedule.changed", {"task_id": task_id, "change": change}))

    def list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"tasks": self.tasks.list(), "now": self._clock.now()}

    def get(self, params: dict[str, Any]) -> dict[str, Any]:
        task = self._task(params.get("task_id"))
        return {"task": self.tasks.view(task), "runs": self._repo.runs(task["id"], limit=20)}

    def create(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = params.get("task")
        if not isinstance(raw, dict):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'task' must be an object.")
        try:
            task = self.tasks.create(raw)
        except ScheduledTaskError as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from None
        self._changed(task["id"], "created")
        return {"task": task}

    def update(self, params: dict[str, Any]) -> dict[str, Any]:
        task = self._task(params.get("task_id"))
        patch = params.get("patch")
        if not isinstance(patch, dict):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'patch' must be an object.")
        try:
            updated = self.tasks.update(task["id"], patch)
        except ScheduledTaskError as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from None
        self._changed(task["id"], "updated")
        return {"task": updated}

    def delete(self, params: dict[str, Any]) -> dict[str, Any]:
        task = self._task(params.get("task_id"))
        self.tasks.delete(task["id"])
        self._changed(task["id"], "deleted")
        return {"deleted": True, "task_id": task["id"]}

    def runs(self, params: dict[str, Any]) -> dict[str, Any]:
        task = self._task(params.get("task_id"))
        limit = params.get("limit", 50)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'limit' must be an int in 1..200.")
        return {"runs": self._repo.runs(task["id"], limit=limit)}

    def run(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.run_now(self._task(params.get("task_id"))["id"])

    def grant(self, params: dict[str, Any]) -> dict[str, Any]:
        """«Permitir para esta tarea»: what a run asked for joins the task's grants.

        By session (the approval card knows its session, not the task) or by
        task id (the detail of a blocked run)."""
        capability = params.get("capability")
        target = params.get("target")
        if not isinstance(capability, str) or not capability:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'capability' must be a string.")
        if target is not None and not isinstance(target, str):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'target' must be a string.")
        task_id = params.get("task_id")
        session_id = params.get("session_id")
        if task_id is None and isinstance(session_id, str) and session_id:
            run = self._repo.run_for_session(session_id)
            if run is None:
                raise NotFoundError(f"Session {session_id} is not a scheduled run.")
            task_id = run["task_id"]
        task = self._task(task_id)
        try:
            updated = self.tasks.add_grant(task["id"], capability, target)
        except ScheduledTaskError as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from None
        self._changed(task["id"], "updated")
        return {"task": updated}


def _wanted(payload: dict[str, Any]) -> str:
    capability = payload.get("capability") or payload.get("tool") or "approval"
    target = payload.get("target")
    return f"{capability}: {target}" if target else str(capability)
