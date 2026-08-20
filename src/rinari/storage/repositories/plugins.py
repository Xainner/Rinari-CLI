"""Plugin registry repository (phase 5)."""

from __future__ import annotations

from rinari.storage.db import Database


class PluginRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        plugin_id: str,
        *,
        name: str,
        version: str,
        source: str,
        scope: str,
        path: str,
        manifest_json: str,
        enabled: bool = True,
        created_at: str = "",
    ) -> dict:
        self._db.execute(
            """
            INSERT INTO plugins (
                id, name, version, source, scope, path, enabled, manifest_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, name) DO UPDATE SET
                version = excluded.version,
                source = excluded.source,
                path = excluded.path,
                manifest_json = excluded.manifest_json,
                updated_at = excluded.created_at
            """,
            (
                plugin_id,
                name,
                version,
                source,
                scope,
                str(path),
                1 if enabled else 0,
                manifest_json,
                created_at,
                created_at,
            ),
        )
        return self.find(name, scope)

    def find(self, name: str, scope: str = "global") -> dict | None:
        rows = self._db.query("SELECT * FROM plugins WHERE scope = ? AND name = ?", (scope, name))
        return dict(rows[0]) if rows else None

    def list(self, scope: str | None = None) -> list[dict]:
        if scope is None:
            rows = self._db.query("SELECT * FROM plugins ORDER BY scope, name")
        else:
            rows = self._db.query("SELECT * FROM plugins WHERE scope = ? ORDER BY name", (scope,))
        return [dict(r) for r in rows]

    def set_enabled(self, name: str, scope: str, enabled: bool, updated_at: str) -> bool:
        cursor = self._db.execute(
            "UPDATE plugins SET enabled = ?, updated_at = ? WHERE scope = ? AND name = ?",
            (1 if enabled else 0, updated_at, scope, name),
        )
        return cursor.rowcount > 0

    def delete(self, name: str, scope: str) -> bool:
        cursor = self._db.execute("DELETE FROM plugins WHERE scope = ? AND name = ?", (scope, name))
        return cursor.rowcount > 0


__all__ = ["PluginRepository"]
