"""Scheduled tasks and their runs (migration 0037).

Rows only: validation and the meaning of each field live in
`rinari.schedule.service`.
"""

from __future__ import annotations

import json
from typing import Any

from rinari.storage.db import Database

_TASK_COLUMNS = (
    "name",
    "kind",
    "schedule_json",
    "prompt",
    "project_id",
    "mode",
    "permission_profile",
    "model",
    "skills_json",
    "grants_json",
    "command_json",
    "tool_json",
    "watch_json",
    "agent_id",
    "target",
    "session_id",
    "enabled",
    "next_run_at",
    "created_at",
    "updated_at",
)
_RUN_COLUMNS = (
    "task_id",
    "status",
    "trigger",
    "scheduled_for",
    "started_at",
    "finished_at",
    "session_id",
    "turn_id",
    "summary",
    "reason",
)


def _task(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["schedule"] = json.loads(item.pop("schedule_json"))
    item["skills"] = json.loads(item.pop("skills_json"))
    item["grants"] = json.loads(item.pop("grants_json"))
    raw_command = item.pop("command_json", None)
    item["command"] = json.loads(raw_command) if raw_command else None
    raw_tool = item.pop("tool_json", None)
    item["tool"] = json.loads(raw_tool) if raw_tool else None
    raw_watch = item.pop("watch_json", None)
    item["watch"] = json.loads(raw_watch) if raw_watch else None
    item["agent_id"] = item.get("agent_id")
    item["target"] = item.get("target") or "new_session"
    item["session_id"] = item.get("session_id")
    item["enabled"] = bool(item["enabled"])
    return item


class ScheduleRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- tasks -------------------------------------------------------------

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
        return _task(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM scheduled_tasks ORDER BY created_at")
        return [_task(row) for row in rows]

    def due(self, now: float) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM scheduled_tasks WHERE enabled = 1 AND next_run_at IS NOT NULL "
            "AND next_run_at <= ? ORDER BY next_run_at",
            (now,),
        )
        return [_task(row) for row in rows]

    def save(self, task_id: str, task: dict[str, Any]) -> dict[str, Any]:
        row = {
            "name": task["name"],
            "kind": task["kind"],
            "schedule_json": json.dumps(task["schedule"], sort_keys=True),
            "prompt": task.get("prompt") or "",
            "project_id": task.get("project_id"),
            "mode": task.get("mode") or "build",
            "permission_profile": task.get("permission_profile"),
            "model": task.get("model"),
            "skills_json": json.dumps(list(task.get("skills") or [])),
            "grants_json": json.dumps(list(task.get("grants") or []), sort_keys=True),
            "command_json": (
                json.dumps(task["command"], sort_keys=True) if task.get("command") else None
            ),
            "tool_json": json.dumps(task["tool"], sort_keys=True) if task.get("tool") else None,
            "watch_json": (
                json.dumps(task["watch"], sort_keys=True) if task.get("watch") else None
            ),
            "agent_id": task.get("agent_id"),
            "target": task.get("target") or "new_session",
            "session_id": task.get("session_id"),
            "enabled": 1 if task.get("enabled", True) else 0,
            "next_run_at": task.get("next_run_at"),
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
        }
        self._db.execute(
            f"""
            INSERT INTO scheduled_tasks (id, {", ".join(_TASK_COLUMNS)})
            VALUES (?, {", ".join("?" for _ in _TASK_COLUMNS)})
            ON CONFLICT(id) DO UPDATE SET
                {", ".join(f"{column} = excluded.{column}" for column in _TASK_COLUMNS)}
            """,
            (task_id, *(row[column] for column in _TASK_COLUMNS)),
        )
        return self.get(task_id) or {}

    def set_enabled(self, task_id: str, enabled: bool, updated_at: float) -> None:
        """Turn a task on or off without validating it again («Detener a todos»)."""
        self._db.execute(
            "UPDATE scheduled_tasks SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), updated_at, task_id),
        )

    def set_watch_fingerprint(self, task_id: str, fingerprint: str) -> None:
        """What a watched folder looked like at its last check (kept across restarts)."""
        task = self.get(task_id)
        if task is None or not task.get("watch"):
            return
        watch = {**task["watch"], "fingerprint": fingerprint}
        self._db.execute(
            "UPDATE scheduled_tasks SET watch_json = ? WHERE id = ?",
            (json.dumps(watch, sort_keys=True), task_id),
        )

    def delete(self, task_id: str) -> bool:
        existed = self.get(task_id) is not None
        with self._db.transaction():
            self._db.execute("DELETE FROM scheduled_runs WHERE task_id = ?", (task_id,))
            self._db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        return existed

    # -- runs --------------------------------------------------------------

    def run(self, run_id: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM scheduled_runs WHERE id = ?", (run_id,))
        return dict(row) if row else None

    def runs(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM scheduled_runs WHERE task_id = ? "
            "ORDER BY COALESCE(started_at, scheduled_for) DESC LIMIT ?",
            (task_id, limit),
        )
        return [dict(row) for row in rows]

    def run_for_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT * FROM scheduled_runs WHERE session_id = ? ORDER BY started_at DESC LIMIT 1",
            (session_id,),
        )
        return dict(row) if row else None

    def has_active_run(self, task_id: str) -> bool:
        """A run of the task still waiting, running or asking, however old it is."""
        row = self._db.query_one(
            "SELECT 1 FROM scheduled_runs WHERE task_id = ? "
            "AND status IN ('queued', 'running', 'needs_you') LIMIT 1",
            (task_id,),
        )
        return row is not None

    def queued_runs_of(self, task_id: str | None = None) -> list[dict[str, Any]]:
        """Runs waiting in a conversation's queue: a task's, or every task's."""
        if task_id is None:
            rows = self._db.query("SELECT * FROM scheduled_runs WHERE status = 'queued'")
        else:
            rows = self._db.query(
                "SELECT * FROM scheduled_runs WHERE task_id = ? AND status = 'queued'",
                (task_id,),
            )
        return [dict(row) for row in rows]

    def unfinished_runs(self) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM scheduled_runs WHERE status IN ('queued', 'running', 'needs_you')"
        )
        return [dict(row) for row in rows]

    def queued_runs(self, session_id: str) -> list[dict[str, Any]]:
        """Runs waiting in a conversation's queue («Repetir aquí»), oldest first."""
        rows = self._db.query(
            "SELECT * FROM scheduled_runs WHERE session_id = ? AND status = 'queued' "
            "ORDER BY scheduled_for, started_at",
            (session_id,),
        )
        return [dict(row) for row in rows]

    def save_run(self, run_id: str, **fields: Any) -> dict[str, Any]:
        unknown = set(fields) - set(_RUN_COLUMNS)
        if unknown:
            raise ValueError(f"unknown run fields: {sorted(unknown)}")
        current = self.run(run_id) or {}
        row = {column: fields.get(column, current.get(column)) for column in _RUN_COLUMNS}
        row["trigger"] = row["trigger"] or "schedule"
        self._db.execute(
            f"""
            INSERT INTO scheduled_runs (id, {", ".join(_RUN_COLUMNS)})
            VALUES (?, {", ".join("?" for _ in _RUN_COLUMNS)})
            ON CONFLICT(id) DO UPDATE SET
                {", ".join(f"{column} = excluded.{column}" for column in _RUN_COLUMNS)}
            """,
            (run_id, *(row[column] for column in _RUN_COLUMNS)),
        )
        return self.run(run_id) or {}

    def last_run(self, task_id: str) -> dict[str, Any] | None:
        runs = self.runs(task_id, limit=1)
        return runs[0] if runs else None
