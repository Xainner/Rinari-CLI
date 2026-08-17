"""Validation record repository (phase 3: evidence for the completion gate)."""

from __future__ import annotations

from rinari.storage.db import Database


class ValidationRecordRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    _FIELDS = (
        "id",
        "project_root",
        "session_ref",
        "kind",
        "command",
        "result",
        "summary",
        "detail",
        "artifact_ref",
        "created_at",
    )

    def insert(self, row: dict) -> dict:
        cols = ", ".join(self._FIELDS)
        placeholders = ", ".join("?" for _ in self._FIELDS)
        self._db.execute(
            f"INSERT INTO validation_records ({cols}) VALUES ({placeholders})",
            tuple(row[field] for field in self._FIELDS),
        )
        return self.get(row["id"]) or dict(row)

    def get(self, record_id: str) -> dict | None:
        row = self._db.query_one("SELECT * FROM validation_records WHERE id = ?", (record_id,))
        return dict(row) if row else None

    def list(
        self,
        project_root: str,
        *,
        kinds: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        query = "SELECT * FROM validation_records WHERE project_root = ?"
        params: list[object] = [project_root]
        if kinds:
            marks = ", ".join("?" for _ in kinds)
            query += f" AND kind IN ({marks})"
            params.extend(kinds)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return [dict(row) for row in self._db.query(query, tuple(params))]

    def latest(self, project_root: str, kind: str) -> dict | None:
        row = self._db.query_one(
            "SELECT * FROM validation_records WHERE project_root = ? AND kind = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (project_root, kind),
        )
        return dict(row) if row else None

    def delete(self, record_id: str) -> bool:
        cursor = self._db.execute("DELETE FROM validation_records WHERE id = ?", (record_id,))
        return cursor.rowcount > 0
