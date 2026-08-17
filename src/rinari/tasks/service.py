"""Task graph service (phase 3): application-level rules over the repository."""

from __future__ import annotations

from pathlib import Path

from rinari.application.context import AppContext
from rinari.shared.clock import now_iso
from rinari.shared.errors import (
    BlockedError,
    ConflictError,
    InvalidUsageError,
    NotFoundError,
    ValidationFailureError,
)
from rinari.shared.ids import TASK
from rinari.storage.repositories.tasks import TaskRepository
from rinari.tasks import core


class TaskService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    @property
    def repo(self) -> TaskRepository:
        return self._ctx.task_repo

    def _root(self, path: str | Path) -> str:
        root = Path(path).expanduser()
        if not root.exists():
            raise InvalidUsageError(
                f"Path does not exist: {root}",
                hint="Point `rinari tasks` at an existing project directory.",
            )
        return str(root.resolve())

    def _get(self, task_id: str) -> dict:
        task = self.repo.get(task_id)
        if task is None:
            raise NotFoundError(
                f"Task not found: {task_id}", hint="List tasks with `rinari tasks list`."
            )
        return task

    # -- queries ----------------------------------------------------------------

    def list(self, path: str | Path) -> list[dict]:
        return self.repo.list(self._root(path))

    def show(self, path: str | Path, task_id: str) -> dict:
        task = self._get(task_id)
        return {**task, "done_when": core.done_when_report(task)}

    def tree(self, path: str | Path) -> dict:
        tasks = self.repo.list(self._root(path))
        return {"tasks": tasks, "depths": core.tree_depths(tasks)}

    def blockers(self, path: str | Path) -> list[dict]:
        tasks = self.repo.list(self._root(path))
        by_id = {t["id"]: t for t in tasks}
        result: list[dict] = []
        for task in tasks:
            if task["status"] in (core.DONE, core.CANCELLED):
                continue
            missing_deps = core.dependencies_ready(task, by_id)
            if task["status"] == core.BLOCKED or missing_deps:
                result.append(
                    {
                        "id": task["id"],
                        "title": task["title"],
                        "status": task["status"],
                        "blocker": task["blockers"] or None,
                        "waiting_on": missing_deps,
                    }
                )
        return result

    # -- mutations -----------------------------------------------------------------

    def add(
        self,
        path: str | Path,
        title: str,
        *,
        description: str = "",
        acceptance: str = "",
        implementation: str = "",
        validation: str = "",
        scope: str = "",
        unresolved: str = "",
        depends_on: str = "",
    ) -> dict:
        title = title.strip()
        if not title:
            raise InvalidUsageError("Task title must not be empty.")
        deps = [d.strip() for d in depends_on.split(",") if d.strip()]
        for dep in deps:
            if self.repo.get(dep) is None:
                raise InvalidUsageError(
                    f"Unknown dependency: {dep}",
                    hint="Dependencies must reference existing task IDs.",
                )
        task_id = self._ctx.ids.new(TASK)
        for dep in deps:
            if core.would_create_cycle(self.repo.list_all(), task_id, dep):
                raise ConflictError(
                    f"Dependency cycle: {task_id} -> {dep}",
                    hint="A task cannot (transitively) depend on itself.",
                )
        now = now_iso(self._ctx.clock)
        return self.repo.create(
            {
                "id": task_id,
                "project_root": self._root(path),
                "session_ref": None,
                "title": title,
                "description": description,
                "status": core.PENDING,
                "acceptance": acceptance,
                "implementation": implementation,
                "validation": validation,
                "scope": scope,
                "unresolved": unresolved,
                "depends_on": ",".join(deps),
                "blockers": "",
                "evidence": "",
                "created_at": now,
                "updated_at": now,
            }
        )

    def update(self, path: str | Path, task_id: str, **fields: str | None) -> dict:
        task = self._get(task_id)
        if "blocker" in fields:
            if fields["blocker"] is not None:
                fields = {**fields, "blockers": fields["blocker"]}
            del fields["blocker"]
        changes = {key: value for key, value in fields.items() if value is not None}
        if not changes:
            raise InvalidUsageError(
                "No fields to update.",
                hint="Pass at least one of --title/--description/--status/--blocker/...",
            )
        if "depends_on" in changes:
            changes["depends_on"] = self._validate_dependencies(task, changes["depends_on"])
        if "status" in changes:
            changes["status"] = self._apply_status(
                task,
                changes.pop("status"),
                fields.get("blocker"),
                project_root=task["project_root"],
            )
        now = now_iso(self._ctx.clock)
        changes["updated_at"] = now
        return self.repo.update(task_id, changes) or task

    def cancel(self, path: str | Path, task_id: str) -> dict:
        task = self._get(task_id)
        if task["status"] == core.DONE:
            raise InvalidUsageError(
                f"Task is already done: {task_id}",
                hint="Use a new task if the work must be redone.",
            )
        return self.repo.update(task_id, self._status_change(core.CANCELLED)) or task

    def retry(self, path: str | Path, task_id: str) -> dict:
        task = self._get(task_id)
        if task["status"] not in (core.BLOCKED, core.CANCELLED):
            raise InvalidUsageError(
                f"Task cannot be retried from status {task['status']!r}.",
                hint="`rinari tasks retry` applies to blocked or cancelled tasks.",
            )
        changes = self._status_change(core.PENDING)
        changes["blockers"] = ""
        return self.repo.update(task_id, changes) or task

    def resolve_blocker(self, path: str | Path, task_id: str, evidence: str) -> dict:
        """Attach evidence refs to a task (semicolon-separated list appends)."""
        task = self._get(task_id)
        evidence = evidence.strip()
        if not evidence:
            raise InvalidUsageError("Evidence reference must not be empty.")
        existing = (task["evidence"] + ";" + evidence).strip(";")
        changes = self._status_change(None)
        changes["evidence"] = existing
        changes["updated_at"] = now_iso(self._ctx.clock)
        return self.repo.update(task_id, changes) or task

    # -- status machine ----------------------------------------------------------------

    def _apply_status(
        self, task: dict, status: str, blocker: str | None, *, project_root: str
    ) -> str:
        status = status.strip().lower()
        del blocker  # applied as a plain field by the caller
        if status not in core.VALID_STATUSES:
            raise InvalidUsageError(
                f"Unknown task status: {status}",
                hint=f"Valid statuses: {', '.join(sorted(core.VALID_STATUSES))}",
            )
        if status == core.IN_PROGRESS:
            missing = core.dependencies_ready(
                task, {t["id"]: t for t in self.repo.list(project_root)}
            )
            if missing:
                raise BlockedError(
                    f"Cannot start {task['id']}: waiting on {', '.join(missing)}",
                    hint="Those dependencies must be done first.",
                )
        if status == core.DONE:
            reasons = core.completion_blockers(task)
            if reasons:
                raise ValidationFailureError(
                    f"Task {task['id']} is not done-when-satisfied: " + "; ".join(reasons),
                    hint="Satisfy the criteria (mark them [x]) or record what is unresolved.",
                )
        return status

    def _validate_dependencies(self, task: dict, depends_on: str | None) -> str:
        deps = [d.strip() for d in (depends_on or "").split(",") if d.strip()]
        for dep in deps:
            if self.repo.get(dep) is None:
                raise InvalidUsageError(
                    f"Unknown dependency: {dep}",
                    hint="Dependencies must reference existing task IDs.",
                )
        rows = []
        for row in self.repo.list(task["project_root"]):
            if row["id"] == task["id"]:
                row = {**row, "depends_on": ""}
            rows.append(row)
        for dep in deps:
            if core.would_create_cycle(rows, task["id"], dep):
                raise ConflictError(
                    f"Dependency cycle: {task['id']} -> {dep}",
                    hint="A task cannot (transitively) depend on itself.",
                )
        return ",".join(deps)

    def _status_change(self, status: str | None) -> dict:
        changes: dict = {}
        if status is not None:
            changes["status"] = status
            changes["updated_at"] = now_iso(self._ctx.clock)
        return changes
