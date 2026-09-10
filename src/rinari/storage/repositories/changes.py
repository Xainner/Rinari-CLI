"""Persistence for per-turn filesystem attribution and undo audit."""

from __future__ import annotations

import json
from typing import Any


class TurnChangeRepository:
    def __init__(self, db) -> None:
        self._db = db

    def insert(self, changeset: dict[str, Any], files: list[dict[str, Any]]) -> None:
        with self._db.transaction():
            self._db.execute(
                """
                INSERT INTO turn_changesets (
                  id, turn_id, session_id, project_id, roots_json, created_at,
                  completed_at, additions, deletions, undoable,
                  attribution_complete, warnings_json, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    changeset["id"],
                    changeset["turn_id"],
                    changeset["session_id"],
                    changeset.get("project_id"),
                    json.dumps(changeset.get("roots", [])),
                    changeset["created_at"],
                    changeset["completed_at"],
                    changeset["additions"],
                    changeset["deletions"],
                    int(changeset["undoable"]),
                    int(changeset["attribution_complete"]),
                    json.dumps(changeset.get("warnings", [])),
                    changeset["status"],
                ),
            )
            for row in files:
                self._db.execute(
                    """
                    INSERT INTO turn_changed_files (
                      changeset_id, path, absolute_path, previous_path, kind,
                      additions, deletions, before_exists, after_exists,
                      before_hash, after_hash, before_size, after_size, ownership,
                      confidence, binary, sensitive, diff_text, diff_truncated,
                      undoable, conflict_reason, before_blob_ref
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        changeset["id"],
                        row["path"],
                        row["absolute_path"],
                        row.get("previous_path"),
                        row["kind"],
                        row.get("additions"),
                        row.get("deletions"),
                        int(row["before_exists"]),
                        int(row["after_exists"]),
                        row.get("before_hash"),
                        row.get("after_hash"),
                        row.get("before_size"),
                        row.get("after_size"),
                        row["ownership"],
                        row["confidence"],
                        int(row["binary"]),
                        int(row["sensitive"]),
                        row.get("diff"),
                        int(row["diff_truncated"]),
                        int(row["undoable"]),
                        row.get("conflict_reason"),
                        row.get("before_blob_ref"),
                    ),
                )

    def get_by_turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM turn_changesets WHERE turn_id = ?", (turn_id,))
        if row is None:
            return None
        return self._hydrate(dict(row))

    def get(self, changeset_id: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM turn_changesets WHERE id = ?", (changeset_id,))
        return self._hydrate(dict(row)) if row is not None else None

    def files(self, changeset_id: str) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM turn_changed_files WHERE changeset_id = ? ORDER BY path",
            (changeset_id,),
        )
        return [self._file(dict(row)) for row in rows]

    def set_status(self, changeset_id: str, status: str) -> None:
        self._db.execute(
            "UPDATE turn_changesets SET status = ? WHERE id = ?", (status, changeset_id)
        )

    def insert_undo(self, row: dict[str, Any]) -> None:
        self._db.execute(
            """INSERT INTO turn_change_undo_operations
            (id, changeset_id, created_at, completed_at, status, applied_json,
             conflicts_json, skipped_json) VALUES (?,?,?,?,?,?,?,?)""",
            (
                row["id"],
                row["changeset_id"],
                row["created_at"],
                row.get("completed_at"),
                row["status"],
                json.dumps(row.get("applied", [])),
                json.dumps(row.get("conflicts", [])),
                json.dumps(row.get("skipped", [])),
            ),
        )

    def _hydrate(self, row: dict[str, Any]) -> dict[str, Any]:
        row["roots"] = json.loads(row.pop("roots_json") or "[]")
        row["warnings"] = json.loads(row.pop("warnings_json") or "[]")
        for key in ("undoable", "attribution_complete"):
            row[key] = bool(row[key])
        row["files"] = self.files(row["id"])
        return row

    @staticmethod
    def _file(row: dict[str, Any]) -> dict[str, Any]:
        for key in (
            "before_exists",
            "after_exists",
            "binary",
            "sensitive",
            "diff_truncated",
            "undoable",
        ):
            row[key] = bool(row[key])
        row["diff"] = row.pop("diff_text")
        return row
