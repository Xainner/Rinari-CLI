"""Follow-up suggestions: worthwhile tasks Rinari notices while she works.

A suggestion is a note for the owner, never work by itself. Rules that keep
notes useful instead of noisy:

- at most 2 per turn, 3 pending per conversation and 5 per project; past
  that the oldest pending one is superseded;
- a title like one suggested in the last 30 days in the same project (or
  conversation, for loose chats) is a duplicate, whatever happened to it;
- pending notes expire after 14 days.

Accepting is the owner's decision: the engine creates the conversation and
starts it (engine_protocol), this service only records the outcome.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from rinari.shared.clock import iso_utc, now_iso

MAX_PER_TURN = 2
MAX_PENDING_PER_SESSION = 3
MAX_PENDING_PER_PROJECT = 5
DUPLICATE_WINDOW = timedelta(days=30)
EXPIRY = timedelta(days=14)
STATUSES = ("pending", "accepted", "dismissed", "superseded", "expired")


class FollowupError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Suggestion:
    id: str
    session_id: str
    turn_id: str | None
    project_id: str | None
    title: str
    prompt: str
    rationale: str
    status: str
    provenance: dict[str, Any]
    accepted_session_id: str | None
    created_at: str
    resolved_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "project_id": self.project_id,
            "title": self.title,
            "prompt": self.prompt,
            "rationale": self.rationale,
            "status": self.status,
            "external_content": bool(self.provenance.get("external_content")),
            "sources": list(self.provenance.get("sources") or [])[:5],
            "accepted_session_id": self.accepted_session_id,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }


def normalized_key(title: str) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s]", " ", text.casefold())
    return " ".join(text.split())


def _clean(value: Any, low: int, high: int, field: str) -> str:
    if not isinstance(value, str):
        raise FollowupError("INVALID", f"{field} must be text")
    text = " ".join(value.split()) if field == "title" else value.strip()
    if not low <= len(text) <= high:
        raise FollowupError("INVALID", f"{field} must be {low}-{high} characters")
    return text


class FollowupService:
    def __init__(self, ctx) -> None:
        self._ctx = ctx
        self._lock = threading.Lock()
        self._current_turn: dict[str, str] = {}
        self._per_turn: dict[str, int] = {}

    # -- turn tracking ----------------------------------------------------------

    def begin_turn(self, session_id: str, turn_id: str) -> None:
        with self._lock:
            self._current_turn[session_id] = turn_id
            self._per_turn[turn_id] = 0

    def end_turn(self, session_id: str) -> None:
        with self._lock:
            turn_id = self._current_turn.pop(session_id, None)
            if turn_id:
                self._per_turn.pop(turn_id, None)

    # -- suggest ------------------------------------------------------------------

    def suggest(
        self,
        session_id: str,
        *,
        title: Any,
        prompt: Any,
        rationale: Any = "",
        provenance: dict[str, Any] | None = None,
    ) -> tuple[Suggestion, list[Suggestion]]:
        """Store a note; returns it and the ones it superseded."""
        clean_title = _clean(title, 3, 80, "title")
        clean_prompt = _clean(prompt, 10, 2000, "prompt")
        clean_rationale = rationale.strip()[:200] if isinstance(rationale, str) else ""
        record = self._ctx.session_repo.get(session_id)
        if record is None:
            raise FollowupError("NOT_FOUND", "unknown conversation")
        key = normalized_key(clean_title)
        now = now_iso(self._ctx.clock)
        with self._lock:
            turn_id = self._current_turn.get(session_id)
            if turn_id is not None and self._per_turn.get(turn_id, 0) >= MAX_PER_TURN:
                raise FollowupError(
                    "LIMIT", f"at most {MAX_PER_TURN} suggestions per turn; keep the best ones"
                )
            if self._duplicate(record, key, now):
                raise FollowupError(
                    "DUPLICATE", "this was already suggested recently; do not suggest it again"
                )
            suggestion_id = self._ctx.ids.new("fup")
            with self._ctx.db.transaction():
                self._ctx.db.execute(
                    "INSERT INTO followup_suggestions (id, session_id, turn_id, project_id, "
                    "title, prompt, rationale, normalized_key, status, provenance_json, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        suggestion_id,
                        record.id,
                        turn_id,
                        record.project_id,
                        clean_title,
                        clean_prompt,
                        clean_rationale,
                        key,
                        json.dumps(provenance or {}, ensure_ascii=False),
                        now,
                    ),
                )
                superseded = self._supersede(record, now)
            if turn_id is not None:
                self._per_turn[turn_id] = self._per_turn.get(turn_id, 0) + 1
        return self.get(suggestion_id), superseded

    def _duplicate(self, record, key: str, now: str) -> bool:
        since = _shift(now, -DUPLICATE_WINDOW)
        if record.project_id:
            row = self._ctx.db.query_one(
                "SELECT 1 FROM followup_suggestions WHERE project_id = ? AND normalized_key = ? "
                "AND created_at >= ?",
                (record.project_id, key, since),
            )
        else:
            row = self._ctx.db.query_one(
                "SELECT 1 FROM followup_suggestions WHERE session_id = ? AND normalized_key = ? "
                "AND created_at >= ?",
                (record.id, key, since),
            )
        return row is not None

    def _supersede(self, record, now: str) -> list[Suggestion]:
        """Keep the pending caps: the oldest notes give way to the new one."""
        superseded: list[Suggestion] = []
        scopes = [("session_id", record.id, MAX_PENDING_PER_SESSION)]
        if record.project_id:
            scopes.append(("project_id", record.project_id, MAX_PENDING_PER_PROJECT))
        for column, value, cap in scopes:
            rows = self._ctx.db.query(
                f"SELECT id FROM followup_suggestions WHERE {column} = ? "
                "AND status = 'pending' ORDER BY created_at DESC, rowid DESC",
                (value,),
            )
            for row in rows[cap:]:
                self._resolve(row["id"], "superseded", now)
                superseded.append(self.get(row["id"]))
        return superseded

    # -- read ---------------------------------------------------------------------

    def get(self, suggestion_id: str) -> Suggestion:
        row = self._ctx.db.query_one(
            "SELECT * FROM followup_suggestions WHERE id = ?", (suggestion_id,)
        )
        if row is None:
            raise FollowupError("NOT_FOUND", "unknown suggestion")
        return _from_row(row)

    def list(
        self,
        *,
        session_id: str | None = None,
        project_id: str | None = None,
        rinari_profile_id: str | None = None,
        status: str | None = "pending",
    ) -> list[Suggestion]:
        self.expire()
        sql = (
            "SELECT f.* FROM followup_suggestions f JOIN sessions s ON s.id = f.session_id "
            "WHERE 1=1"
        )
        params: list[Any] = []
        if session_id is not None:
            sql += " AND f.session_id = ?"
            params.append(session_id)
        if project_id is not None:
            sql += " AND f.project_id = ?"
            params.append(project_id)
        if rinari_profile_id is not None:
            sql += " AND s.rinari_profile_id = ?"
            params.append(rinari_profile_id)
        if status is not None:
            sql += " AND f.status = ?"
            params.append(status)
        sql += " ORDER BY f.created_at DESC, f.rowid DESC"
        return [_from_row(row) for row in self._ctx.db.query(sql, params)]

    def expire(self) -> int:
        now = now_iso(self._ctx.clock)
        return self._ctx.db.execute(
            "UPDATE followup_suggestions SET status = 'expired', resolved_at = ? "
            "WHERE status = 'pending' AND created_at < ?",
            (now, _shift(now, -EXPIRY)),
        ).rowcount

    # -- resolve ----------------------------------------------------------------------

    def dismiss(self, suggestion_id: str) -> Suggestion:
        current = self.get(suggestion_id)
        if current.status != "pending":
            return current
        self._resolve(suggestion_id, "dismissed", now_iso(self._ctx.clock))
        return self.get(suggestion_id)

    def mark_accepted(self, suggestion_id: str, session_id: str) -> Suggestion:
        now = now_iso(self._ctx.clock)
        self._ctx.db.execute(
            "UPDATE followup_suggestions SET status = 'accepted', resolved_at = ?, "
            "accepted_session_id = ? WHERE id = ?",
            (now, session_id, suggestion_id),
        )
        return self.get(suggestion_id)

    def _resolve(self, suggestion_id: str, status: str, now: str) -> None:
        self._ctx.db.execute(
            "UPDATE followup_suggestions SET status = ?, resolved_at = ? WHERE id = ?",
            (status, now, suggestion_id),
        )


def _shift(iso: str, delta: timedelta) -> str:
    """The same ISO format as now_iso, so string order stays chronological."""
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return iso_utc((moment + delta).timestamp())


def _from_row(row) -> Suggestion:
    try:
        provenance = json.loads(row["provenance_json"] or "{}")
    except ValueError:
        provenance = {}
    return Suggestion(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        turn_id=row["turn_id"],
        project_id=row["project_id"],
        title=str(row["title"]),
        prompt=str(row["prompt"]),
        rationale=str(row["rationale"] or ""),
        status=str(row["status"]),
        provenance=provenance if isinstance(provenance, dict) else {},
        accepted_session_id=row["accepted_session_id"],
        created_at=str(row["created_at"]),
        resolved_at=row["resolved_at"],
    )
