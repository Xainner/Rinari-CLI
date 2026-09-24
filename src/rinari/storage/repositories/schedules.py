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
    "model",
    "skills_json",
    "grants_json",
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
            "model": task.get("model"),
            "skills_json": json.dumps(list(task.get("skills") or [])),
            "grants_json": json.dumps(list(task.get("grants") or []), sort_keys=True),
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

    def unfinished_runs(self) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM scheduled_runs WHERE status IN ('running', 'needs_you')"
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
