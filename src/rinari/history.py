"""Historial de conversaciones en SQLite.

Cada sesión tiene un id, perfil, created_at y una lista de mensajes JSON.
Persistencia: ~/.rinari/history.sqlite por defecto.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class HistoryError(Exception):
    """Error de historial (sesión inexistente, etc.)."""


class History:
    def __init__(self, db_path: Path | str | None = None):
        if db_path is None:
            db_path = Path.home() / ".rinari" / "history.sqlite"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
                """
            )

    def create_session(self, profile: str = "default") -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO sessions (profile, created_at) VALUES (?, ?)",
                (profile, now),
            )
            return int(cur.lastrowid)

    def append_message(self, session_id: int, message: dict) -> None:
        exists = self._conn.execute(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not exists:
            raise HistoryError(f"La sesión {session_id} no existe")
        seq = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        with self._conn:
            self._conn.execute(
                "INSERT INTO messages (session_id, role, content, seq) VALUES (?, ?, ?, ?)",
                (session_id, message.get("role", "user"), json.dumps(message), seq),
            )

    def get_messages(self, session_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT content FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [json.loads(r["content"]) for r in rows]

    def list_sessions(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT s.id, s.profile, s.created_at, COUNT(m.id) as message_count
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            GROUP BY s.id
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "profile": r["profile"],
                "created_at": r["created_at"],
                "message_count": r["message_count"],
            }
            for r in rows
        ]

    def delete_session(self, session_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def export_session(self, session_id: int) -> str:
        """Exporta una sesión a markdown legible (para guardar/compartir)."""
        row = self._conn.execute(
            "SELECT id, profile, created_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            raise HistoryError(f"La sesión {session_id} no existe")
        messages = self.get_messages(session_id)
        lines = [
            f"# Conversación {session_id} — perfil '{row['profile']}'",
            f"_Creada: {row['created_at']}_",
            "",
        ]
        for m in messages:
            role = m.get("role", "user")
            label = "Usuario" if role == "user" else "Rinari"
            content = m.get("content", "")
            lines.append(f"**{label}:**")
            lines.append(content)
            lines.append("")
        return "\n".join(lines).strip()

    def close(self) -> None:
        self._conn.close()
