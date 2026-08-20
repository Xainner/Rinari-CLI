"""MCP server registry repository (phase 5)."""

from __future__ import annotations

from rinari.storage.db import Database


class McpServerRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        server_id: str,
        *,
        name: str,
        transport: str,
        command: str = "",
        url: str = "",
        scope: str = "global",
        enabled: bool = True,
        config_json: str = "",
        created_at: str = "",
    ) -> dict:
        self._db.execute(
            """
            INSERT INTO mcp_servers (
                id, name, transport, command, url, scope, enabled, config_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, name) DO UPDATE SET
                transport = excluded.transport,
                command = excluded.command,
                url = excluded.url,
                enabled = excluded.enabled,
                config_json = excluded.config_json,
                updated_at = excluded.created_at
            """,
            (
                server_id,
                name,
                transport,
                command,
                url,
                scope,
                1 if enabled else 0,
                config_json,
                created_at,
                created_at,
            ),
        )
        return self.find(name, scope)

    def find(self, name: str, scope: str = "global") -> dict | None:
        rows = self._db.query(
            "SELECT * FROM mcp_servers WHERE scope = ? AND name = ?", (scope, name)
        )
        return dict(rows[0]) if rows else None

    def list(self, scope: str | None = None) -> list[dict]:
        if scope is None:
            rows = self._db.query("SELECT * FROM mcp_servers ORDER BY scope, name")
        else:
            rows = self._db.query(
                "SELECT * FROM mcp_servers WHERE scope = ? ORDER BY name", (scope,)
            )
        return [dict(r) for r in rows]

    def set_enabled(self, name: str, scope: str, enabled: bool, updated_at: str) -> bool:
        cursor = self._db.execute(
            "UPDATE mcp_servers SET enabled = ?, updated_at = ? WHERE scope = ? AND name = ?",
            (1 if enabled else 0, updated_at, scope, name),
        )
        return cursor.rowcount > 0

    def delete(self, name: str, scope: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM mcp_servers WHERE scope = ? AND name = ?", (scope, name)
        )
        return cursor.rowcount > 0


__all__ = ["McpServerRepository"]
