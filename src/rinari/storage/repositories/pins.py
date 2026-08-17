"""Context pin repository (phase 4): session-scoped pinned context pointers."""

from __future__ import annotations

from rinari.storage.db import Database

PIN_SOURCES = ("file", "symbol", "memory", "artifact", "term")


class PinRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def pin(self, session_ref: str, source: str, pin_ref: str, label: str, created_at: str) -> None:
        self._db.execute(
            """
            INSERT INTO context_pins (session_ref, source, pin_ref, label, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_ref, source, pin_ref) DO UPDATE SET
                label = excluded.label
            """,
            (session_ref, source, pin_ref, label, created_at),
        )

    def unpin(self, session_ref: str, source: str, pin_ref: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM context_pins WHERE session_ref = ? AND source = ? AND pin_ref = ?",
            (session_ref, source, pin_ref),
        )
        return cursor.rowcount > 0

    def list(self, session_ref: str) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM context_pins WHERE session_ref = ? ORDER BY created_at, pin_ref",
            (session_ref,),
        )
        return [dict(r) for r in rows]

    def delete_session(self, session_ref: str) -> int:
        cursor = self._db.execute("DELETE FROM context_pins WHERE session_ref = ?", (session_ref,))
        return cursor.rowcount


__all__ = ["PIN_SOURCES", "PinRepository"]
