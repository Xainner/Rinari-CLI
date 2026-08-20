"""OpenAPI spec registry repository (phase 5)."""

from __future__ import annotations

from rinari.storage.db import Database


class ApiSpecRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        spec_id: str,
        *,
        name: str,
        origin: str,
        path: str = "",
        url: str = "",
        scope: str = "global",
        enabled: bool = True,
        auth_json: str = "",
        overrides_json: str = "",
        spec_hash: str = "",
        created_at: str = "",
    ) -> dict:
        self._db.execute(
            """
            INSERT INTO api_specs (
                id, name, origin, path, url, scope, enabled, auth_json,
                overrides_json, spec_hash, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, name) DO UPDATE SET
                origin = excluded.origin,
                path = excluded.path,
                url = excluded.url,
                enabled = excluded.enabled,
                auth_json = excluded.auth_json,
                overrides_json = excluded.overrides_json,
                spec_hash = excluded.spec_hash,
                updated_at = excluded.created_at
            """,
            (
                spec_id,
                name,
                origin,
                path,
                url,
                scope,
                1 if enabled else 0,
                auth_json,
                overrides_json,
                spec_hash,
                created_at,
                created_at,
            ),
        )
        return self.find(name, scope)

    def find(self, name: str, scope: str = "global") -> dict | None:
        rows = self._db.query("SELECT * FROM api_specs WHERE scope = ? AND name = ?", (scope, name))
        return dict(rows[0]) if rows else None

    def list(self, scope: str | None = None) -> list[dict]:
        if scope is None:
            rows = self._db.query("SELECT * FROM api_specs ORDER BY scope, name")
        else:
            rows = self._db.query("SELECT * FROM api_specs WHERE scope = ? ORDER BY name", (scope,))
        return [dict(r) for r in rows]

    def set_enabled(self, name: str, scope: str, enabled: bool, updated_at: str) -> bool:
        cursor = self._db.execute(
            "UPDATE api_specs SET enabled = ?, updated_at = ? WHERE scope = ? AND name = ?",
            (1 if enabled else 0, updated_at, scope, name),
        )
        return cursor.rowcount > 0

    def set_hash(self, name: str, scope: str, spec_hash: str, updated_at: str) -> bool:
        cursor = self._db.execute(
            "UPDATE api_specs SET spec_hash = ?, updated_at = ? WHERE scope = ? AND name = ?",
            (spec_hash, updated_at, scope, name),
        )
        return cursor.rowcount > 0

    def delete(self, name: str, scope: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM api_specs WHERE scope = ? AND name = ?", (scope, name)
        )
        return cursor.rowcount > 0


__all__ = ["ApiSpecRepository"]
