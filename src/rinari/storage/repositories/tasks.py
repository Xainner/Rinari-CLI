"""Task graph repository (phase 3)."""

from __future__ import annotations

from rinari.storage.db import Database


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    _FIELDS = (
        "id",
        "project_root",
        "session_ref",
        "title",
        "description",
        "status",
        "acceptance",
        "implementation",
        "validation",
        "scope",
        "unresolved",
        "depends_on",
        "blockers",
        "evidence",
        "created_at",
        "updated_at",
    )

    def create(self, row: dict) -> dict:
        self._db.execute(
            """
            INSERT INTO tasks (
                id, project_root, session_ref, title, description, status,
                acceptance, implementation, validation, scope, unresolved,
                depends_on, blockers, evidence, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(row[field] for field in self._FIELDS),
        )
        return self.get(row["id"]) or dict(row)

    def get(self, task_id: str) -> dict | None:
        row = self._db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return dict(row) if row else None

    def update(self, task_id: str, fields: dict) -> dict | None:
        if not fields:
            return self.get(task_id)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        params = (*fields.values(), task_id)
        self._db.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", params)
        return self.get(task_id)

    def list(self, project_root: str) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM tasks WHERE project_root = ? ORDER BY created_at, id",
            (project_root,),
        )
        return [dict(row) for row in rows]

    def list_all(self) -> list[dict]:
        rows = self._db.query("SELECT * FROM tasks ORDER BY created_at, id")
        return [dict(row) for row in rows]

    def delete(self, task_id: str) -> bool:
        cursor = self._db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cursor.rowcount > 0
