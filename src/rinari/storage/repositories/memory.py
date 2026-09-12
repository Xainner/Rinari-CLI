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
                superseded_by, created_at, updated_at, revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                row.get("revision", 1),
            ),
        )

    def user_get(self, memory_id: str) -> dict | None:
        row = self._db.query_one(
            "SELECT * FROM user_memory WHERE id = ? AND superseded_by IS NULL", (memory_id,)
        )
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
            f"UPDATE user_memory SET {assignments}, updated_at = ?, "
            "revision = revision + 1 WHERE id = ?",
            tuple(params),
        )

    def suppression_exists(self, text_hash: str) -> bool:
        row = self._db.query_one(
            "SELECT 1 FROM memory_suppressions WHERE text_hash = ?",
            (text_hash,),
        )
        return row is not None

    def suppression_insert(
        self, *, topic_hash: str, text_hash: str, created_at: str
    ) -> None:
        self._db.execute(
            """
            INSERT OR IGNORE INTO memory_suppressions
                (topic_hash, text_hash, created_at)
            VALUES (?, ?, ?)
            """,
            (topic_hash, text_hash, created_at),
        )

    def record_suppression_insert(self, memory_id: str, created_at: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO memory_record_suppressions(memory_id, created_at) "
            "VALUES (?, ?)",
            (memory_id, created_at),
        )

    def record_suppression_exists(self, memory_id: str) -> bool:
        return self._db.query_one(
            "SELECT 1 FROM memory_record_suppressions WHERE memory_id = ?", (memory_id,)
        ) is not None

    def user_lineage_ids(self, memory_id: str) -> set[str]:
        rows = self._db.query(
            """
            WITH RECURSIVE lineage(id) AS (
                SELECT ?
                UNION ALL
                SELECT user_memory.id
                FROM user_memory JOIN lineage ON user_memory.superseded_by = lineage.id
            )
            SELECT id FROM lineage
            """,
            (memory_id,),
        )
        return {str(row["id"]) for row in rows}

    def user_delete(self, memory_id: str) -> bool:
        cursor = self._db.execute("DELETE FROM user_memory WHERE id = ?", (memory_id,))
        return cursor.rowcount > 0

    # -- selective-memory provenance and privacy controls -----------------

    def source_insert(self, row: dict) -> None:
        self._db.execute(
            """
            INSERT OR IGNORE INTO memory_sources
                (id, memory_id, session_id, message_id, source_hash, quote, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"], row["memory_id"], row["session_id"], row["message_id"],
                row["source_hash"], row["quote"], row["created_at"],
            ),
        )

    def sources_for_memory(self, memory_id: str, *, live_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM memory_sources WHERE memory_id = ?"
        params: list[Any] = [memory_id]
        if live_only:
            sql += " AND revoked_at IS NULL"
        sql += " ORDER BY created_at, id"
        return [dict(row) for row in self._db.query(sql, params)]

    def sources_for_session(self, session_id: str, *, live_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM memory_sources WHERE session_id = ?"
        params: list[Any] = [session_id]
        if live_only:
            sql += " AND revoked_at IS NULL"
        sql += " ORDER BY created_at, id"
        return [dict(row) for row in self._db.query(sql, params)]

    def sources_for_message(
        self, session_id: str, message_id: str, *, live_only: bool = False
    ) -> list[dict]:
        sql = "SELECT * FROM memory_sources WHERE session_id = ? AND message_id = ?"
        params: list[Any] = [session_id, message_id]
        if live_only:
            sql += " AND revoked_at IS NULL"
        sql += " ORDER BY created_at, id"
        return [dict(row) for row in self._db.query(sql, params)]

    def source_revoke(self, source_id: str, revoked_at: str) -> None:
        self._db.execute(
            "UPDATE memory_sources SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
            (revoked_at, source_id),
        )

    def source_revoke_for_session(self, session_id: str, revoked_at: str) -> None:
        self._db.execute(
            "UPDATE memory_sources SET revoked_at = COALESCE(revoked_at, ?) "
            "WHERE session_id = ? AND revoked_at IS NULL",
            (revoked_at, session_id),
        )

    def source_suppression_insert(self, session_id: str, message_id: str, created_at: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO memory_source_suppressions(session_id, message_id, created_at) "
            "VALUES (?, ?, ?)",
            (session_id, message_id, created_at),
        )

    def source_suppressed(self, session_id: str, message_id: str) -> bool:
        return self._db.query_one(
            "SELECT 1 FROM memory_source_suppressions WHERE session_id = ? AND message_id = ?",
            (session_id, message_id),
        ) is not None

    def suppressed_message_ids(self, session_id: str) -> set[str]:
        rows = self._db.query(
            "SELECT message_id FROM memory_source_suppressions WHERE session_id = ?",
            (session_id,),
        )
        return {str(row["message_id"]) for row in rows}

    def control(self, session_id: str) -> dict | None:
        row = self._db.query_one(
            "SELECT * FROM memory_conversation_controls WHERE session_id = ?", (session_id,)
        )
        return dict(row) if row else None

    def set_control(self, session_id: str, mode: str, timestamp: str) -> None:
        if mode not in ("auto", "excluded", "deleted"):
            raise ValueError("invalid memory conversation mode")
        self._db.execute(
            """
            INSERT INTO memory_conversation_controls(session_id, mode, excluded_at, deleted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                mode = excluded.mode,
                excluded_at = COALESCE(
                    memory_conversation_controls.excluded_at, excluded.excluded_at
                ),
                deleted_at = COALESCE(memory_conversation_controls.deleted_at, excluded.deleted_at)
            """,
            (
                session_id,
                mode,
                timestamp if mode in ("excluded", "deleted") else None,
                timestamp if mode == "deleted" else None,
            ),
        )

    def candidates(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM memory_candidates"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at, id LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return [dict(row) for row in self._db.query(sql, params)]

    def candidate_get(self, candidate_id: str) -> dict | None:
        row = self._db.query_one("SELECT * FROM memory_candidates WHERE id = ?", (candidate_id,))
        return dict(row) if row else None

    def candidate_insert(self, row: dict) -> None:
        self._db.execute(
            """
            INSERT OR IGNORE INTO memory_candidates
              (id, session_id, message_id, topic, text, kind, confidence,
               classification, reason, status, memory_id, created_at, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"], row["session_id"], row["message_id"], row["topic"], row["text"],
                row["kind"], row["confidence"], row["classification"], row.get("reason", ""),
                row.get("status", "pending"), row.get("memory_id"), row["created_at"],
                row.get("resolved_at"),
            ),
        )

    def candidate_resolve(
        self, candidate_id: str, *, status: str, memory_id: str | None, resolved_at: str
    ) -> None:
        self._db.execute(
            "UPDATE memory_candidates SET status = ?, memory_id = ?, resolved_at = ? WHERE id = ?",
            (status, memory_id, resolved_at, candidate_id),
        )

    def delete_source_rows(self, session_id: str) -> None:
        self._db.execute("DELETE FROM memory_sources WHERE session_id = ?", (session_id,))
        self._db.execute(
            "DELETE FROM memory_source_suppressions WHERE session_id = ?", (session_id,)
        )
        self._db.execute("DELETE FROM memory_candidates WHERE session_id = ?", (session_id,))

    def delete_episodic_for_session(self, session_id: str) -> int:
        cursor = self._db.execute(
            "DELETE FROM episodic_memory WHERE session_ref = ?", (session_id,)
        )
        return int(cursor.rowcount)

    def ledger_watermark(self) -> int:
        row = self._db.query_one("SELECT watermark FROM memory_privacy_ledger WHERE id = 1")
        return int(row["watermark"]) if row else 0

    def ledger_advance(self) -> int:
        self._db.execute(
            "UPDATE memory_privacy_ledger SET watermark = watermark + 1 WHERE id = 1"
        )
        return self.ledger_watermark()

    def ledger_set_max(self, watermark: int) -> int:
        if watermark < 0:
            raise ValueError("ledger watermark must be non-negative")
        self._db.execute(
            "UPDATE memory_privacy_ledger SET watermark = MAX(watermark, ?) WHERE id = 1",
            (watermark,),
        )
        return self.ledger_watermark()

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
