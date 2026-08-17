"""Checkpoint repository (phase 3: `rinari undo`)."""

from __future__ import annotations

import base64

from rinari.storage.db import Database


class CheckpointRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    _CHECKPOINT_FIELDS = (
        "id",
        "session_ref",
        "project_root",
        "label",
        "agent_changes",
        "user_owned",
        "created_at",
    )

    def insert(self, row: dict) -> dict:
        cols = ", ".join(self._CHECKPOINT_FIELDS)
        marks = ", ".join("?" for _ in self._CHECKPOINT_FIELDS)
        self._db.execute(
            f"INSERT INTO checkpoints ({cols}) VALUES ({marks})",
            tuple(row[field] for field in self._CHECKPOINT_FIELDS),
        )
        return self.get(row["id"]) or dict(row)

    def get(self, checkpoint_id: str) -> dict | None:
        row = self._db.query_one("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,))
        return dict(row) if row else None

    def list(self, project_root: str, *, limit: int = 50) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM checkpoints WHERE project_root = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (project_root, max(1, min(limit, 200))),
        )
        return [dict(row) for row in rows]

    def latest(self, project_root: str) -> dict | None:
        row = self._db.query_one(
            "SELECT * FROM checkpoints WHERE project_root = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (project_root,),
        )
        return dict(row) if row else None

    def files(self, checkpoint_id: str) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM checkpoint_files WHERE checkpoint_id = ? ORDER BY path",
            (checkpoint_id,),
        )
        out = []
        for row in rows:
            data = dict(row)
            if data.get("prior_blob"):
                data["prior_bytes"] = base64.b64decode(data.pop("prior_blob"))
            else:
                data["prior_bytes"] = None
            out.append(data)
        return out

    def insert_files(self, checkpoint_id: str, entries) -> None:
        self._db.executemany(
            """
            INSERT INTO checkpoint_files
                (checkpoint_id, path, ownership, prior_source, prior_status, prior_blob)
            VALUES (?, ?, ?, 'snapshot', ?, ?)
            """,
            [
                (
                    checkpoint_id,
                    entry.path,
                    entry.ownership,
                    entry.prior_status,
                    base64.b64encode(entry.prior_bytes).decode()
                    if entry.prior_bytes is not None
                    else None,
                )
                for entry in entries
            ],
        )

    def remove(self, checkpoint_id: str) -> bool:
        existed = self.get(checkpoint_id) is not None
        self._db.execute("DELETE FROM checkpoint_files WHERE checkpoint_id = ?", (checkpoint_id,))
        cursor = self._db.execute("DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,))
        existed = existed and cursor.rowcount > 0
        return existed
