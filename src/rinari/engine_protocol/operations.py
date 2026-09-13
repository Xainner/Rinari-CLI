"""Persistent dispatch identities; uncertain operations are never replayed."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError

MEMORY_ORIGINS = frozenset({"interactive", "owner_channel", "automation"})


class OperationStore:
    def __init__(self, root: Path) -> None:
        self.path = root / "engine-operations.sqlite"
        self.instance = uuid.uuid4().hex
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise RuntimeError("Unsupported operation schema")
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                instance TEXT NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL,
                memory_origin TEXT)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(operations)")}
            if "memory_origin" not in columns:
                db.execute("ALTER TABLE operations ADD COLUMN memory_origin TEXT")
            db.execute("PRAGMA user_version=2")

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, operation_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        return self.project(row) if row else None

    def project(self, row: sqlite3.Row) -> dict[str, Any]:
        state = row["state"]
        if state in {"running", "cancelling"} and row["instance"] != self.instance:
            state = "uncertain"
        return {
            key: row[key] for key in ("operation_id", "session_id", "turn_id", "updated")
        } | {"state": state, "memory_origin": row["memory_origin"]}

    def claim(
        self,
        operation_id: str,
        session_id: str,
        message: str,
        reasoning_effort: str | None,
        turn_id: str,
        remote_target: dict | None = None,
        channel: dict | None = None,
        attachments: list | None = None,
        *,
        memory_origin: str = "automation",
    ) -> tuple[dict[str, Any], bool]:
        if memory_origin not in MEMORY_ORIGINS:
            raise EngineProtocolError(INVALID_PARAMS, "Invalid memory origin")

        def fingerprint(origin: str | None) -> str:
            parts = (
                [session_id, message, reasoning_effort]
                + ([remote_target] if remote_target else [])
                + ([{"channel": channel}] if channel else [])
                + ([{"attachments": attachments}] if attachments else [])
            )
            if origin is not None:
                parts.append({"memory_origin": origin})
            return hashlib.sha256(
                json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()

        operation_fingerprint = fingerprint(memory_origin)
        legacy_fingerprint = fingerprint(None)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row:
                # Rows created by schema v1 have no origin. They can be
                # replayed only against their old identity fingerprint and
                # are never re-executed; new rows require the origin to
                # remain part of the operation identity.
                if row["memory_origin"] is None:
                    matches = row["fingerprint"] == legacy_fingerprint
                else:
                    matches = (
                        row["fingerprint"] == operation_fingerprint
                        and row["memory_origin"] == memory_origin
                    )
                if not matches:
                    raise EngineProtocolError(INVALID_PARAMS, "Operation identity conflict")
                return self.project(row), False
            db.execute(
                "INSERT INTO operations "
                "(operation_id,fingerprint,session_id,turn_id,instance,state,updated,"
                "memory_origin) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    operation_fingerprint,
                    session_id,
                    turn_id,
                    self.instance,
                    "running",
                    time.time(),
                    memory_origin,
                ),
            )
        return self.get(operation_id) or {}, True

    def finish(self, turn_id: str, state: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE operations SET state=?, updated=? WHERE turn_id=? "
                "AND instance=? AND state IN ('running', 'cancelling')",
                (state, time.time(), turn_id, self.instance),
            )
