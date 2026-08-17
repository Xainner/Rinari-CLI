"""Network policy repository (phase 4): persistent rules + decision audit."""

from __future__ import annotations

from rinari.storage.db import Database

RULE_DECISIONS = ("allow", "deny")
RULE_SCOPES = ("global", "project")


class NetworkRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- rules ----------------------------------------------------------------

    def add_rule(
        self,
        rule_id: str,
        host: str,
        decision: str,
        *,
        scope: str = "global",
        project_id: str | None = None,
        reason: str = "",
        created_at: str = "",
    ) -> dict:
        self._db.execute(
            """
            INSERT INTO network_rules (
                id, scope, project_id, host, decision, reason, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, host, decision) DO UPDATE SET
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (rule_id, scope, project_id, host, decision, reason, created_at, created_at),
        )
        return self.find_rule(host, decision, scope=scope)

    def find_rule(self, host: str, decision: str, scope: str = "global") -> dict | None:
        rows = self._db.query(
            """
            SELECT * FROM network_rules
            WHERE scope = ? AND host = ? AND decision = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (scope, host, decision),
        )
        return dict(rows[0]) if rows else None

    def list_rules(self, scope: str | None = None) -> list[dict]:
        if scope is None:
            rows = self._db.query("SELECT * FROM network_rules ORDER BY scope, host, decision")
        else:
            rows = self._db.query(
                "SELECT * FROM network_rules WHERE scope = ? ORDER BY host, decision", (scope,)
            )
        return [dict(r) for r in rows]

    def delete_rule(self, host: str, decision: str, scope: str = "global") -> bool:
        cursor = self._db.execute(
            "DELETE FROM network_rules WHERE scope = ? AND host = ? AND decision = ?",
            (scope, host, decision),
        )
        return cursor.rowcount > 0

    # -- events -----------------------------------------------------------------

    def add_event(
        self,
        event_id: str,
        *,
        host: str,
        action: str,
        reason: str = "",
        session_id: str = "",
        tool: str = "",
        created_at: str = "",
    ) -> None:
        self._db.execute(
            """
            INSERT INTO network_events (id, session_id, tool, host, action, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, session_id, tool, host, action, reason, created_at),
        )

    def list_events(self, limit: int = 20, session_id: str | None = None) -> list[dict]:
        if session_id is None:
            rows = self._db.query(
                "SELECT * FROM network_events ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            )
        else:
            rows = self._db.query(
                """
                SELECT * FROM network_events
                WHERE session_id = ? ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (session_id, limit),
            )
        return [dict(r) for r in rows]

    def count_events(self) -> int:
        rows = self._db.query("SELECT COUNT(*) AS n FROM network_events")
        return int(rows[0]["n"]) if rows else 0


__all__ = ["RULE_DECISIONS", "RULE_SCOPES", "NetworkRepository"]
