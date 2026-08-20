"""Lifecycle hooks registry repository (phase 5)."""

from __future__ import annotations

from rinari.storage.db import Database


class HookRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        hook_id: str,
        *,
        name: str,
        event: str,
        source: str,
        scope: str,
        handler_type: str,
        handler_ref: str,
        capabilities_json: str = "[]",
        risk: str = "low",
        enabled: bool = True,
        created_at: str = "",
    ) -> dict:
        self._db.execute(
            """
            INSERT INTO hooks (
                id, name, event, source, scope, handler_type, handler_ref,
                capabilities_json, risk, enabled, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, source, name) DO UPDATE SET
                event = excluded.event,
                handler_type = excluded.handler_type,
                handler_ref = excluded.handler_ref,
                capabilities_json = excluded.capabilities_json,
                risk = excluded.risk,
                enabled = excluded.enabled,
                updated_at = excluded.created_at
            """,
            (
                hook_id,
                name,
                event,
                source,
                scope,
                handler_type,
                handler_ref,
                capabilities_json,
                risk,
                1 if enabled else 0,
                created_at,
                created_at,
            ),
        )
        return self.find(name, source, scope)

    def find(self, name: str, source: str, scope: str = "global") -> dict | None:
        rows = self._db.query(
            "SELECT * FROM hooks WHERE scope = ? AND source = ? AND name = ?",
            (scope, source, name),
        )
        return dict(rows[0]) if rows else None

    def list(self, event: str | None = None, scope: str | None = None) -> list[dict]:
        clauses = []
        params: list[object] = []
        if event is not None:
            clauses.append("event = ?")
            params.append(event)
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.query(
            f"SELECT * FROM hooks{where} ORDER BY event, source, name", tuple(params)
        )
        return [dict(r) for r in rows]

    def set_enabled(
        self, name: str, source: str, scope: str, enabled: bool, updated_at: str
    ) -> bool:
        cursor = self._db.execute(
            (
                "UPDATE hooks SET enabled = ?, updated_at = ? "
                "WHERE scope = ? AND source = ? AND name = ?"
            ),
            (1 if enabled else 0, updated_at, scope, source, name),
        )
        return cursor.rowcount > 0

    def delete(self, name: str, source: str, scope: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM hooks WHERE scope = ? AND source = ? AND name = ?",
            (scope, source, name),
        )
        return cursor.rowcount > 0


__all__ = ["HookRepository"]
