"""The owner's answers about the MCP servers a project folder brings (C4)."""

from __future__ import annotations

from rinari.storage.db import Database


class McpProjectOfferRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def for_project(self, project_root: str) -> dict[str, dict]:
        rows = self._db.query(
            "SELECT * FROM mcp_project_offers WHERE project_root = ? ORDER BY server_key",
            (project_root,),
        )
        return {row["server_key"]: dict(row) for row in rows}

    def record(
        self,
        project_root: str,
        server_key: str,
        *,
        source: str,
        fingerprint: str,
        decision: str,
        server_name: str,
        decided_at: str,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO mcp_project_offers (
                project_root, server_key, source, fingerprint, decision, server_name,
                decided_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_root, server_key) DO UPDATE SET
                source = excluded.source,
                fingerprint = excluded.fingerprint,
                decision = excluded.decision,
                server_name = excluded.server_name,
                decided_at = excluded.decided_at
            """,
            (project_root, server_key, source, fingerprint, decision, server_name, decided_at),
        )


__all__ = ["McpProjectOfferRepository"]
