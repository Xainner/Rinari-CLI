"""Memory repositories (phase 4): user, project, episodic, pattern stores.

Kept deliberately separate (harness.md section 69): never one shared bucket.
Reads return only live rows (superseded rows are history, not truth).
"""

from __future__ import annotations

from typing import Any

from rinari.storage.db import Database

USER_KINDS = ("preference", "rule", "fact")
PROJECT_KINDS = ("fact", "rule", "convention")


class MemoryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- user memory ---------------------------------------------------------

    def user_insert(self, row: dict) -> None:
        self._db.execute(
            """
            INSERT INTO user_memory (
                id, kind, topic, text, provenance, confidence,
                superseded_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["kind"],
                row["topic"],
                row["text"],
                row.get("provenance", ""),
                row.get("confidence", 1.0),
                row.get("superseded_by"),
                row["created_at"],
                row["updated_at"],
            ),
        )

    def user_get(self, memory_id: str) -> dict | None:
        row = self._db.query_one("SELECT * FROM user_memory WHERE id = ?", (memory_id,))
        return dict(row) if row else None

    def user_live(self) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM user_memory WHERE superseded_by IS NULL ORDER BY updated_at DESC, id"
        )
        return [dict(r) for r in rows]

    def user_search(self, query: str, *, kind: str | None = None, limit: int = 20) -> list[dict]:
        query = (query or "").strip()
        like = f"%{query}%"
        sql = (
            "SELECT * FROM user_memory "
            "WHERE superseded_by IS NULL AND (text LIKE ? OR topic LIKE ?)"
        )
        params: list[Any] = [like, like]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY updated_at DESC, id LIMIT ?"
        params.append(max(1, min(limit, 200)))
        return [dict(r) for r in self._db.query(sql, tuple(params))]

    def user_supersede(self, memory_id: str, superseded_by: str, updated_at: str) -> None:
        self._db.execute(
            "UPDATE user_memory SET superseded_by = ?, updated_at = ? "
            "WHERE id = ? AND superseded_by IS NULL",
            (superseded_by, updated_at, memory_id),
        )

    def user_update(self, memory_id: str, fields: dict, updated_at: str) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        params = (*fields.values(), updated_at, memory_id)
        self._db.execute(
            f"UPDATE user_memory SET {assignments}, updated_at = ? WHERE id = ?",
            tuple(params),
        )

    def user_delete(self, memory_id: str) -> bool:
        cursor = self._db.execute("DELETE FROM user_memory WHERE id = ?", (memory_id,))
        return cursor.rowcount > 0

    # -- project memory ------------------------------------------------------

    def project_insert(self, row: dict) -> None:
        self._db.execute(
            """
            INSERT INTO project_memory (
                id, project_root, kind, topic, text, provenance, confidence,
                superseded_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["project_root"],
                row["kind"],
                row["topic"],
                row["text"],
                row.get("provenance", ""),
                row.get("confidence", 1.0),
                row.get("superseded_by"),
                row["created_at"],
                row["updated_at"],
            ),
        )

    def project_get(self, project_root: str, memory_id: str) -> dict | None:
        row = self._db.query_one(
            "SELECT * FROM project_memory WHERE project_root = ? AND id = ?",
            (project_root, memory_id),
        )
        return dict(row) if row else None

    def project_live(self, project_root: str) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM project_memory WHERE project_root = ? AND superseded_by IS NULL "
            "ORDER BY updated_at DESC, id",
            (project_root,),
        )
        return [dict(r) for r in rows]

    def project_search(
        self, project_root: str, query: str, *, kind: str | None = None, limit: int = 20
    ) -> list[dict]:
        query = (query or "").strip()
        like = f"%{query}%"
        sql = (
            "SELECT * FROM project_memory "
            "WHERE project_root = ? AND superseded_by IS NULL "
            "AND (text LIKE ? OR topic LIKE ?)"
        )
        params: list[Any] = [project_root, like, like]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY updated_at DESC, id LIMIT ?"
        params.append(max(1, min(limit, 200)))
        return [dict(r) for r in self._db.query(sql, tuple(params))]

    def project_update(
        self, project_root: str, memory_id: str, fields: dict, updated_at: str
    ) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        params = (*fields.values(), updated_at, project_root, memory_id)
        self._db.execute(
            f"UPDATE project_memory SET {assignments}, updated_at = ? "
            f"WHERE project_root = ? AND id = ?",
            tuple(params),
        )

    def project_supersede(
        self, project_root: str, memory_id: str, superseded_by: str, updated_at: str
    ) -> None:
        self._db.execute(
            "UPDATE project_memory SET superseded_by = ?, updated_at = ? "
            "WHERE project_root = ? AND id = ? AND superseded_by IS NULL",
            (superseded_by, updated_at, project_root, memory_id),
        )

    def project_delete(self, project_root: str, memory_id: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM project_memory WHERE project_root = ? AND id = ?",
            (project_root, memory_id),
        )
        return cursor.rowcount > 0

    # -- episodic memory -------------------------------------------------------

    def episodic_insert(self, row: dict) -> None:
        self._db.execute(
            """
            INSERT INTO episodic_memory (
                id, session_ref, project_root, summary, outcome, provenance,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["session_ref"],
                row.get("project_root", ""),
                row["summary"],
                row.get("outcome", ""),
                row.get("provenance", ""),
                row["created_at"],
            ),
        )

    def episodic_match(self, session_id, project_root, summary, outcome):
        row = self._db.query_one(
            "SELECT id FROM episodic_memory WHERE session_ref = ? AND project_root = ? "
            "AND summary = ? AND outcome = ? LIMIT 1",
            (session_id, project_root, summary, outcome),
        )
        return dict(row) if row else None

    def episodic_list(self, project_root: str | None = None, *, limit: int = 20) -> list[dict]:
        if project_root:
            rows = self._db.query(
                "SELECT * FROM episodic_memory WHERE project_root = ? "
                "ORDER BY created_at DESC, id LIMIT ?",
                (project_root, max(1, min(limit, 200))),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM episodic_memory ORDER BY created_at DESC, id LIMIT ?",
                (max(1, min(limit, 200)),),
            )
        return [dict(r) for r in rows]

    def episodic_search(self, project_root: str, query: str, *, limit: int = 10) -> list[dict]:
        like = f"%{(query or '').strip()}%"
        rows = self._db.query(
            "SELECT * FROM episodic_memory WHERE project_root = ? AND summary LIKE ? "
            "ORDER BY created_at DESC, id LIMIT ?",
            (project_root, like, max(1, min(limit, 100))),
        )
        return [dict(r) for r in rows]

    # -- pattern memory --------------------------------------------------------

    def pattern_insert(self, row: dict) -> None:
        self._db.execute(
            """
            INSERT INTO pattern_memory (
                id, scope, topic, text, provenance, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row.get("scope", "global"),
                row["topic"],
                row["text"],
                row.get("provenance", ""),
                row["created_at"],
            ),
        )

    def pattern_list(self, *, scope: str | None = None, limit: int = 20) -> list[dict]:
        if scope:
            rows = self._db.query(
                "SELECT * FROM pattern_memory WHERE scope = ? ORDER BY created_at DESC, id LIMIT ?",
                (scope, max(1, min(limit, 200))),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM pattern_memory ORDER BY created_at DESC, id LIMIT ?",
                (max(1, min(limit, 200)),),
            )
        return [dict(r) for r in rows]

    def pattern_delete(self, memory_id: str) -> bool:
        cursor = self._db.execute("DELETE FROM pattern_memory WHERE id = ?", (memory_id,))
        return cursor.rowcount > 0

    def pattern_search(
        self, query: str, *, scope: str | None = None, limit: int = 10
    ) -> list[dict]:
        like = f"%{(query or '').strip()}%"
        if scope:
            rows = self._db.query(
                "SELECT * FROM pattern_memory WHERE scope = ? AND (text LIKE ? OR topic LIKE ?) "
                "ORDER BY created_at DESC, id LIMIT ?",
                (scope, like, like, max(1, min(limit, 100))),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM pattern_memory WHERE (text LIKE ? OR topic LIKE ?) "
                "ORDER BY created_at DESC, id LIMIT ?",
                (like, like, max(1, min(limit, 100))),
            )
        return [dict(r) for r in rows]


__all__ = ["PROJECT_KINDS", "USER_KINDS", "MemoryRepository"]
