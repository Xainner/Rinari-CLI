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
            if version not in (0, 1, 2, 3):
                raise RuntimeError("Unsupported operation schema")
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                instance TEXT NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL,
                memory_origin TEXT)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(operations)")}
            if "memory_origin" not in columns:
                db.execute("ALTER TABLE operations ADD COLUMN memory_origin TEXT")
            # Schema 3: peer groups and the durable session inbox. Accepted peer
            # deliveries survive a restart as paused rows; they are never
            # replayed automatically.
            db.execute("""CREATE TABLE IF NOT EXISTS peer_groups (
                group_id TEXT PRIMARY KEY, board_id TEXT NOT NULL,
                owner TEXT NOT NULL, revision INTEGER NOT NULL,
                authorization_epoch INTEGER NOT NULL, instance TEXT NOT NULL,
                enabled INTEGER NOT NULL, updated REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS peer_group_members (
                group_id TEXT NOT NULL, session_id TEXT NOT NULL, label TEXT NOT NULL,
                send INTEGER NOT NULL, receive INTEGER NOT NULL, updated REAL NOT NULL,
                PRIMARY KEY (group_id, session_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS session_inbox (
                message_id TEXT PRIMARY KEY, seq INTEGER NOT NULL,
                target_session_id TEXT NOT NULL, source_session_id TEXT,
                source_turn_id TEXT, group_id TEXT, group_revision INTEGER,
                authorization_epoch INTEGER, chain_id TEXT, hop INTEGER NOT NULL,
                origin_json TEXT NOT NULL, content TEXT NOT NULL,
                display_message TEXT, state TEXT NOT NULL,
                operation_id TEXT UNIQUE, turn_id TEXT, dedupe_key TEXT UNIQUE,
                enqueue_policy TEXT NOT NULL, error TEXT, instance TEXT,
                created REAL NOT NULL, updated REAL NOT NULL)""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS session_inbox_target "
                "ON session_inbox (target_session_id, state, seq)"
            )
            db.execute("""CREATE TABLE IF NOT EXISTS peer_chain_counters (
                chain_id TEXT PRIMARY KEY, deliveries INTEGER NOT NULL,
                updated REAL NOT NULL)""")
            db.execute("PRAGMA user_version=3")

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
        return {key: row[key] for key in ("operation_id", "session_id", "turn_id", "updated")} | {
            "state": state,
            "memory_origin": row["memory_origin"],
        }

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

    # -- peer groups ---------------------------------------------------------

    def group_get(self, group_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM peer_groups WHERE group_id=?", (group_id,)).fetchone()
            if not row:
                return None
            members = db.execute(
                "SELECT * FROM peer_group_members WHERE group_id=? ORDER BY session_id",
                (group_id,),
            ).fetchall()
        return self._project_group(row, members)

    def group_for_board(self, board_id: str, owner: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT group_id FROM peer_groups WHERE board_id=? AND owner=?", (board_id, owner)
            ).fetchone()
        return self.group_get(row["group_id"]) if row else None

    def group_for_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT g.group_id FROM peer_group_members m JOIN peer_groups g "
                "ON g.group_id = m.group_id WHERE m.session_id=? AND g.enabled=1 "
                "ORDER BY g.updated DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return self.group_get(row["group_id"]) if row else None

    @staticmethod
    def _project_group(row: sqlite3.Row, members: list[sqlite3.Row]) -> dict[str, Any]:
        return {
            "group_id": row["group_id"],
            "board_id": row["board_id"],
            "owner": row["owner"],
            "revision": row["revision"],
            "authorization_epoch": row["authorization_epoch"],
            "instance": row["instance"],
            "enabled": bool(row["enabled"]),
            "members": [
                {
                    "session_id": member["session_id"],
                    "label": member["label"],
                    "send": bool(member["send"]),
                    "receive": bool(member["receive"]),
                }
                for member in members
            ],
        }

    def group_set(
        self,
        *,
        group_id: str | None,
        board_id: str,
        owner: str,
        expected_revision: int,
        enabled: bool,
        members: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomic replace of a group's membership guarded by `expected_revision`.

        Creating a group (`group_id=None`, `expected_revision=0`) is idempotent
        per `(board_id, owner)`: a retry with the same composition returns the
        existing group untouched; a different composition is a CONFLICT that
        names the live revision. Changes that widen or shrink authorization (a
        member added or removed, a flag enabled, the group re-enabled) bump the
        authorization epoch so grants issued against the previous composition
        stop matching. A session belongs to at most one enabled group: it is
        displaced from any other one (that group's epoch bumps as well) and the
        result carries the displaced group ids under `displaced`.
        """
        params_group_id = group_id
        displaced: list[str] = []
        unchanged = False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if group_id is not None:
                row = db.execute(
                    "SELECT * FROM peer_groups WHERE group_id=?", (group_id,)
                ).fetchone()
                if not row:
                    raise EngineProtocolError("NOT_FOUND", f"Peer group not found: {group_id}")
            else:
                row = db.execute(
                    "SELECT * FROM peer_groups WHERE board_id=? AND owner=?", (board_id, owner)
                ).fetchone()
            now = time.time()
            if row is None:
                if expected_revision != 0:
                    raise EngineProtocolError(
                        "CONFLICT", "Peer group does not exist yet; expected_revision must be 0"
                    )
                group_id = "group_" + uuid.uuid4().hex
                revision = 1
                epoch = 1
                db.execute(
                    "INSERT INTO peer_groups (group_id, board_id, owner, revision, "
                    "authorization_epoch, instance, enabled, updated) VALUES (?,?,?,?,?,?,?,?)",
                    (group_id, board_id, owner, revision, epoch, self.instance, int(enabled), now),
                )
            else:
                group_id = row["group_id"]
                if row["owner"] != owner:
                    raise EngineProtocolError(
                        "PERMISSION_DENIED", "Peer group belongs to another client"
                    )
                previous = {
                    member["session_id"]: member
                    for member in db.execute(
                        "SELECT * FROM peer_group_members WHERE group_id=?", (group_id,)
                    ).fetchall()
                }
                if expected_revision == 0 and not params_group_id:
                    # Retry of a create: nothing to change, no revision bump.
                    unchanged = bool(row["enabled"]) == enabled and {
                        (m["session_id"], bool(m["send"]), bool(m["receive"]))
                        for m in previous.values()
                    } == {(m["session_id"], m["send"], m["receive"]) for m in members}
                if unchanged:
                    members = []
                else:
                    if row["revision"] != expected_revision:
                        raise EngineProtocolError(
                            "CONFLICT",
                            f"Peer group revision is {row['revision']}, "
                            f"expected {expected_revision}",
                            details={"group_id": group_id, "revision": row["revision"]},
                        )
                    revision = row["revision"] + 1
                    epoch = row["authorization_epoch"]
                    widened = (not row["enabled"] and enabled) or any(
                        member["session_id"] not in previous
                        or (member["send"] and not previous[member["session_id"]]["send"])
                        or (member["receive"] and not previous[member["session_id"]]["receive"])
                        for member in members
                    )
                    removed = set(previous) - {member["session_id"] for member in members}
                    if widened or removed:
                        epoch += 1
                    db.execute(
                        "UPDATE peer_groups SET revision=?, authorization_epoch=?, instance=?, "
                        "enabled=?, updated=? WHERE group_id=?",
                        (revision, epoch, self.instance, int(enabled), now, group_id),
                    )
                    db.execute("DELETE FROM peer_group_members WHERE group_id=?", (group_id,))
            for member in members:
                db.execute(
                    "INSERT INTO peer_group_members (group_id, session_id, label, send, receive, "
                    "updated) VALUES (?,?,?,?,?,?)",
                    (
                        group_id,
                        member["session_id"],
                        member["label"],
                        int(member["send"]),
                        int(member["receive"]),
                        now,
                    ),
                )
                # One enabled group per session: leave any other one.
                others = db.execute(
                    "SELECT m.group_id FROM peer_group_members m JOIN peer_groups g "
                    "ON g.group_id = m.group_id WHERE m.session_id=? AND m.group_id<>?",
                    (member["session_id"], group_id),
                ).fetchall()
                for other in others:
                    db.execute(
                        "DELETE FROM peer_group_members WHERE group_id=? AND session_id=?",
                        (other["group_id"], member["session_id"]),
                    )
                    db.execute(
                        "UPDATE peer_groups SET revision=revision+1, "
                        "authorization_epoch=authorization_epoch+1, updated=? WHERE group_id=?",
                        (now, other["group_id"]),
                    )
                    if other["group_id"] not in displaced:
                        displaced.append(other["group_id"])
        result = self.group_get(group_id) or {}
        return {**result, "displaced": displaced, "unchanged": unchanged}

    def group_revoke(self, group_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM peer_groups WHERE group_id=?", (group_id,)).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE peer_groups SET enabled=0, revision=revision+1, "
                "authorization_epoch=authorization_epoch+1, updated=? WHERE group_id=?",
                (time.time(), group_id),
            )
            db.execute(
                "UPDATE session_inbox SET state='paused', error='group revoked', updated=? "
                "WHERE group_id=? AND state='queued'",
                (time.time(), group_id),
            )
        return self.group_get(group_id)

    def group_drop_session(self, session_id: str) -> list[str]:
        """A closed/archived/deleted session leaves every group; returns affected group ids."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT DISTINCT group_id FROM peer_group_members WHERE session_id=?", (session_id,)
            ).fetchall()
            ids = [row["group_id"] for row in rows]
            for group_id in ids:
                db.execute(
                    "DELETE FROM peer_group_members WHERE group_id=? AND session_id=?",
                    (group_id, session_id),
                )
                db.execute(
                    "UPDATE peer_groups SET revision=revision+1, "
                    "authorization_epoch=authorization_epoch+1, updated=? WHERE group_id=?",
                    (time.time(), group_id),
                )
            db.execute(
                "UPDATE session_inbox SET state='cancelled', error='session gone', updated=? "
                "WHERE (target_session_id=? OR source_session_id=?) "
                "AND state IN ('queued', 'paused')",
                (time.time(), session_id, session_id),
            )
        return ids

    # -- session inbox --------------------------------------------------------

    def chain_reserve(self, chain_id: str, limit: int) -> int:
        """Reserve one delivery slot on a chain; raises when the cap is reached."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT deliveries FROM peer_chain_counters WHERE chain_id=?", (chain_id,)
            ).fetchone()
            count = (row["deliveries"] if row else 0) + 1
            if count > limit:
                raise EngineProtocolError(
                    "RESOURCE_EXHAUSTED",
                    f"Peer chain delivered {limit} messages; no further deliveries admitted.",
                    details={"reason": "PEER_BUDGET_EXHAUSTED", "chain_id": chain_id},
                )
            db.execute(
                "INSERT INTO peer_chain_counters (chain_id, deliveries, updated) VALUES (?,?,?) "
                "ON CONFLICT(chain_id) DO UPDATE SET deliveries=excluded.deliveries, "
                "updated=excluded.updated",
                (chain_id, count, time.time()),
            )
        return count

    def inbox_add(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Insert an accepted delivery. A repeated `dedupe_key` returns the prior row."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if entry.get("dedupe_key"):
                existing = db.execute(
                    "SELECT * FROM session_inbox WHERE dedupe_key=?", (entry["dedupe_key"],)
                ).fetchone()
                if existing:
                    return self._project_inbox(existing) | {"duplicate": True}
            seq_row = db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM session_inbox"
            ).fetchone()
            now = time.time()
            db.execute(
                "INSERT INTO session_inbox (message_id, seq, target_session_id, "
                "source_session_id, source_turn_id, group_id, group_revision, "
                "authorization_epoch, chain_id, hop, origin_json, content, display_message, "
                "state, operation_id, turn_id, dedupe_key, enqueue_policy, error, instance, "
                "created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry["message_id"],
                    seq_row["n"],
                    entry["target_session_id"],
                    entry.get("source_session_id"),
                    entry.get("source_turn_id"),
                    entry.get("group_id"),
                    entry.get("group_revision"),
                    entry.get("authorization_epoch"),
                    entry.get("chain_id"),
                    int(entry.get("hop", 0)),
                    json.dumps(entry["origin"], ensure_ascii=False, sort_keys=True),
                    entry["content"],
                    entry.get("display_message"),
                    entry.get("state", "queued"),
                    entry.get("operation_id"),
                    None,
                    entry.get("dedupe_key"),
                    entry.get("enqueue_policy", "start_when_idle"),
                    None,
                    self.instance,
                    now,
                    now,
                ),
            )
        return self.inbox_get(entry["message_id"]) or {}

    def inbox_get(self, message_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM session_inbox WHERE message_id=?", (message_id,)
            ).fetchone()
        return self._project_inbox(row) if row else None

    def inbox_list(
        self, target_session_id: str, states: tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        with self.connect() as db:
            if states:
                placeholders = ",".join("?" for _ in states)
                rows = db.execute(
                    "SELECT * FROM session_inbox WHERE target_session_id=? "
                    f"AND state IN ({placeholders}) ORDER BY seq",
                    (target_session_id, *states),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM session_inbox WHERE target_session_id=? ORDER BY seq",
                    (target_session_id,),
                ).fetchall()
        return [self._project_inbox(row) for row in rows]

    def inbox_claim_next(self, target_session_id: str, turn_id: str) -> dict[str, Any] | None:
        """Claim the oldest queued delivery for a session in one transaction."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM session_inbox WHERE target_session_id=? AND state='queued' "
                "ORDER BY seq LIMIT 1",
                (target_session_id,),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE session_inbox SET state='dispatching', turn_id=?, instance=?, updated=? "
                "WHERE message_id=?",
                (turn_id, self.instance, time.time(), row["message_id"]),
            )
            claimed = db.execute(
                "SELECT * FROM session_inbox WHERE message_id=?", (row["message_id"],)
            ).fetchone()
        return self._project_inbox(claimed)

    def inbox_update(self, message_id: str, state: str, *, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE session_inbox SET state=?, error=?, updated=? WHERE message_id=?",
                (state, error, time.time(), message_id),
            )

    def inbox_finish_turn(self, turn_id: str, state: str) -> list[str]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT message_id FROM session_inbox WHERE turn_id=? "
                "AND state IN ('dispatching', 'running')",
                (turn_id,),
            ).fetchall()
            db.execute(
                "UPDATE session_inbox SET state=?, updated=? WHERE turn_id=? "
                "AND state IN ('dispatching', 'running')",
                (state, time.time(), turn_id),
            )
        return [row["message_id"] for row in rows]

    def inbox_pause_session(self, target_session_id: str, reason: str) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE session_inbox SET state='paused', error=?, updated=? "
                "WHERE target_session_id=? AND state='queued'",
                (reason, time.time(), target_session_id),
            )
        return cursor.rowcount

    def inbox_resume_session(self, target_session_id: str) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE session_inbox SET state='queued', error=NULL, updated=? "
                "WHERE target_session_id=? AND state='paused'",
                (time.time(), target_session_id),
            )
        return cursor.rowcount

    def inbox_cancel(self, message_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM session_inbox WHERE message_id=?", (message_id,)
            ).fetchone()
            if not row or row["state"] not in ("queued", "paused"):
                return self._project_inbox(row) if row else None
            db.execute(
                "UPDATE session_inbox SET state='cancelled', updated=? WHERE message_id=?",
                (time.time(), message_id),
            )
        return self.inbox_get(message_id)

    def inbox_recover_after_restart(self) -> int:
        """Rows claimed by another instance become uncertain; queued rows pause.

        Nothing is replayed: reanudar es una intervención explícita del cliente.
        """
        with self.connect() as db:
            uncertain = db.execute(
                "UPDATE session_inbox SET state='uncertain', "
                "error='claimed by a previous engine instance', updated=? "
                "WHERE state IN ('dispatching', 'running') AND instance<>?",
                (time.time(), self.instance),
            )
            paused = db.execute(
                "UPDATE session_inbox SET state='paused', error='engine restarted', updated=? "
                "WHERE state='queued'",
                (time.time(),),
            )
        return uncertain.rowcount + paused.rowcount

    def _project_inbox(self, row: sqlite3.Row) -> dict[str, Any]:
        state = row["state"]
        if state in {"dispatching", "running"} and row["instance"] != self.instance:
            state = "uncertain"
        return {
            "message_id": row["message_id"],
            "seq": row["seq"],
            "target_session_id": row["target_session_id"],
            "source_session_id": row["source_session_id"],
            "source_turn_id": row["source_turn_id"],
            "group_id": row["group_id"],
            "group_revision": row["group_revision"],
            "authorization_epoch": row["authorization_epoch"],
            "chain_id": row["chain_id"],
            "hop": row["hop"],
            "dedupe_key": row["dedupe_key"],
            "origin": json.loads(row["origin_json"]),
            "content": row["content"],
            "display_message": row["display_message"],
            "state": state,
            "operation_id": row["operation_id"],
            "turn_id": row["turn_id"],
            "enqueue_policy": row["enqueue_policy"],
            "error": row["error"],
            "created": row["created"],
            "updated": row["updated"],
        }
