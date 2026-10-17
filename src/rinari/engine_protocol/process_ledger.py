"""A durable list of the processes the model started (plan 0.4.0 #90).

The registry of a conversation lives in memory: after the Engine restarts
(or dies) the Terminal panel no longer showed what was still running, and the
model started the same server again on the same port. Each process is written
here when it starts and marked when it ends; at the next start the ones still
alive (same PID and same start time) are orphans the panel shows with «Detener».
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from rinari.tools.native import process_identity


class ProcessLedger:
    def __init__(self, root: Path) -> None:
        self.path = root / "engine-processes.sqlite"
        self.instance = uuid.uuid4().hex
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS processes (
                    key TEXT PRIMARY KEY, instance TEXT NOT NULL, session_id TEXT NOT NULL,
                    handle TEXT NOT NULL, pid INTEGER NOT NULL, os_started REAL,
                    command TEXT NOT NULL, cwd TEXT NOT NULL, name TEXT, port INTEGER,
                    persist INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, ended REAL)"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS processes_open ON processes (ended)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def key(self, session_id: str, handle: str) -> str:
        return f"{self.instance}:{session_id}:{handle}"

    def record(self, session_id: str, handle: Any) -> None:
        pid = int(handle.process.pid)
        with self._lock, contextlib.suppress(sqlite3.Error), self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO processes (key, instance, session_id, handle, pid, "
                "os_started, command, cwd, name, port, persist, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.key(session_id, handle.id),
                    self.instance,
                    session_id,
                    handle.id,
                    pid,
                    process_identity.started_at(pid),
                    _text(handle.command),
                    handle.cwd or "",
                    getattr(handle, "name", None),
                    getattr(handle, "port", None),
                    1 if getattr(handle, "persist", False) else 0,
                    time.time(),
                ),
            )

    def ended(self, session_id: str, handle_id: str) -> None:
        with self._lock, contextlib.suppress(sqlite3.Error), self._connect() as db:
            db.execute(
                "UPDATE processes SET ended = ? WHERE key = ? AND ended IS NULL",
                (time.time(), self.key(session_id, handle_id)),
            )

    def orphans(self) -> list[dict[str, Any]]:
        """Processes of earlier Engines that are still alive; the dead ones are closed."""
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM processes WHERE ended IS NULL AND instance != ? ORDER BY created",
                (self.instance,),
            ).fetchall()
            alive: list[dict[str, Any]] = []
            for row in rows:
                if process_identity.is_same(int(row["pid"]), row["os_started"]):
                    alive.append(_orphan(row))
                else:
                    db.execute(
                        "UPDATE processes SET ended = ? WHERE key = ?", (time.time(), row["key"])
                    )
            return alive

    def stop_orphan(self, key: str) -> bool:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM processes WHERE key = ? AND ended IS NULL AND instance != ?",
                (key, self.instance),
            ).fetchone()
            if row is None:
                return False
            if process_identity.is_same(int(row["pid"]), row["os_started"]):
                process_identity.kill_tree(int(row["pid"]))
            db.execute("UPDATE processes SET ended = ? WHERE key = ?", (time.time(), key))
            return True


def _orphan(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["key"],
        "session_id": row["session_id"],
        "handle": row["handle"],
        "pid": row["pid"],
        "command": row["command"],
        "cwd": row["cwd"],
        "name": row["name"],
        "port": row["port"],
        "persist": bool(row["persist"]),
        "started_at": row["created"],
    }


def _text(command: Any) -> str:
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command)
