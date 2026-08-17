import json

from rinari.storage.db import Database
from rinari.storage.records import (
    SessionEventRecord,
    SessionMessageRecord,
    SessionRecord,
)


class SessionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def insert(self, rec: SessionRecord) -> None:
        self._db.execute(
            """
            INSERT INTO sessions (
                id, kind, title, project_id, project_root_snapshot,
                created_cwd, current_cwd, provider_id, model_id,
                profile_id, mode, state, compact_state_json,
                created_at, updated_at, last_active_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.id,
                rec.kind,
                rec.title,
                rec.project_id,
                rec.project_root_snapshot,
                rec.created_cwd,
                rec.current_cwd,
                rec.provider_id,
                rec.model_id,
                rec.profile_id,
                rec.mode,
                rec.state,
                json.dumps(rec.compact_state, sort_keys=True) if rec.compact_state else None,
                rec.created_at,
                rec.updated_at,
                rec.last_active_at,
            ),
        )

    def get(self, session_id: str) -> SessionRecord | None:
        row = self._db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return _session_to_record(row) if row else None

    def update(self, rec: SessionRecord) -> None:
        self._db.execute(
            """
            UPDATE sessions SET
                kind = ?, title = ?, project_id = ?, project_root_snapshot = ?,
                current_cwd = ?, provider_id = ?, model_id = ?, profile_id = ?,
                mode = ?, state = ?, compact_state_json = ?, updated_at = ?, last_active_at = ?
            WHERE id = ?
            """,
            (
                rec.kind,
                rec.title,
                rec.project_id,
                rec.project_root_snapshot,
                rec.current_cwd,
                rec.provider_id,
                rec.model_id,
                rec.profile_id,
                rec.mode,
                rec.state,
                json.dumps(rec.compact_state, sort_keys=True) if rec.compact_state else None,
                rec.updated_at,
                rec.last_active_at,
                rec.id,
            ),
        )

    def list(
        self,
        kind: str | None = None,
        project_id: str | None = None,
        limit: int | None = None,
    ) -> list[SessionRecord]:
        sql = "SELECT * FROM sessions WHERE 1=1"
        params: list[object] = []
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " ORDER BY last_active_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_session_to_record(r) for r in self._db.query(sql, params)]


def _session_to_record(row: dict) -> SessionRecord:
    compact = row["compact_state_json"]
    return SessionRecord(
        id=row["id"],
        kind=row["kind"],
        title=row["title"],
        project_id=row["project_id"],
        project_root_snapshot=row["project_root_snapshot"],
        created_cwd=row["created_cwd"],
        current_cwd=row["current_cwd"],
        provider_id=row["provider_id"],
        model_id=row["model_id"],
        profile_id=row["profile_id"],
        mode=row["mode"],
        state=row["state"],
        compact_state=json.loads(compact) if compact else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_active_at=row["last_active_at"],
    )


class SessionEventRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def next_seq(self, session_id: str) -> int:
        row = self._db.query_one(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM session_events WHERE session_id = ?",
            (session_id,),
        )
        return int(row["n"]) if row else 1

    def insert(self, rec: SessionEventRecord) -> None:
        self._db.execute(
            """
            INSERT INTO session_events (id, session_id, seq, type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rec.id,
                rec.session_id,
                rec.seq,
                rec.type,
                json.dumps(rec.payload, sort_keys=True),
                rec.created_at,
            ),
        )

    def list(
        self, session_id: str, after_seq: int | None = None, limit: int | None = None
    ) -> list[SessionEventRecord]:
        sql = "SELECT * FROM session_events WHERE session_id = ?"
        params: list[object] = [session_id]
        if after_seq is not None:
            sql += " AND seq > ?"
            params.append(after_seq)
        sql += " ORDER BY seq ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_event_to_record(r) for r in self._db.query(sql, params)]


def _event_to_record(row: dict) -> SessionEventRecord:
    return SessionEventRecord(
        id=row["id"],
        session_id=row["session_id"],
        seq=row["seq"],
        type=row["type"],
        payload=json.loads(row["payload_json"]) if row["payload_json"] else {},
        created_at=row["created_at"],
    )


class SessionMessageRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def next_seq(self, session_id: str) -> int:
        row = self._db.query_one(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM session_messages WHERE session_id = ?",
            (session_id,),
        )
        return int(row["n"]) if row else 1

    def append_many(self, session_id: str, records: list[SessionMessageRecord]) -> None:
        start = self.next_seq(session_id)
        for offset, rec in enumerate(records):
            rec.seq = start + offset
            self._db.execute(
                """
                INSERT INTO session_messages (
                    id, session_id, seq, role, content,
                    tool_calls_json, tool_call_id, name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.id,
                    session_id,
                    rec.seq,
                    rec.role,
                    rec.content,
                    json.dumps(rec.tool_calls, sort_keys=True) if rec.tool_calls else None,
                    rec.tool_call_id,
                    rec.name,
                    rec.created_at,
                ),
            )

    def list(self, session_id: str) -> list[SessionMessageRecord]:
        rows = self._db.query(
            "SELECT * FROM session_messages WHERE session_id = ? ORDER BY seq ASC",
            (session_id,),
        )
        return [_message_to_record(r) for r in rows]


def _message_to_record(row: dict) -> SessionMessageRecord:
    return SessionMessageRecord(
        id=row["id"],
        session_id=row["session_id"],
        seq=row["seq"],
        role=row["role"],
        content=row["content"],
        tool_calls=json.loads(row["tool_calls_json"]) if row["tool_calls_json"] else None,
        tool_call_id=row["tool_call_id"],
        name=row["name"],
        created_at=row["created_at"],
    )
