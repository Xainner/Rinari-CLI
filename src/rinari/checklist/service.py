"""The live checklist of a conversation.

The model keeps it with `checklist.update` (the whole list each time) while
it works on something with several steps. The point is that it stays true:

- an item is `completed` only when the model says so after doing it;
- when a turn ends, a list the turn worked on is settled from the outcome:
  every item done -> `completed`; a normal ending with items left -> `open`;
  a cancelled, failed or stopped turn with items left -> `interrupted`;
- the next turn clears a `completed` list (`cleared`) and tells the model
  about an `open` or `interrupted` one, so it reconciles it instead of
  leaving a stale list on screen.

One row per session holds the current list (`session_checklists`); every
change is also a turn event (`checklist.updated`), which is its history.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from rinari.shared.clock import now_iso

CHECKLIST_MAX_ITEMS = 20
ITEM_STATUSES = ("pending", "in_progress", "completed", "blocked")
_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
_STATES = ("active", "open", "interrupted", "completed", "cleared")


class ChecklistError(ValueError):
    """A checklist the model sent cannot be stored as it is."""


@dataclass(frozen=True, slots=True)
class Checklist:
    session_id: str
    turn_id: str | None
    revision: int
    state: str
    items: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    explanation: str = ""
    updated_at: str = ""

    def counts(self) -> dict[str, int]:
        counts = {status: 0 for status in ITEM_STATUSES}
        for item in self.items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        counts["total"] = len(self.items)
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "revision": self.revision,
            "state": self.state,
            "items": [dict(item) for item in self.items],
            "explanation": self.explanation,
            "counts": self.counts(),
            "updated_at": self.updated_at,
        }


def validate_items(raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Normalized items plus non-fatal warnings; ChecklistError when unusable."""
    if not isinstance(raw, list):
        raise ChecklistError("items must be a list")
    if len(raw) > CHECKLIST_MAX_ITEMS:
        raise ChecklistError(f"at most {CHECKLIST_MAX_ITEMS} items")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ChecklistError(f"item {index} must be an object")
        item_id = entry.get("id")
        if not isinstance(item_id, str) or not _ID.fullmatch(item_id):
            raise ChecklistError(f"item {index}: id must be 1-32 letters, digits, '-' or '_'")
        if item_id in seen:
            raise ChecklistError(f"duplicate item id {item_id!r}")
        seen.add(item_id)
        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ChecklistError(f"item {item_id}: content is required")
        content = " ".join(content.split())
        if len(content) > 200:
            raise ChecklistError(f"item {item_id}: content is longer than 200 characters")
        status = entry.get("status")
        if status not in ITEM_STATUSES:
            allowed = ", ".join(ITEM_STATUSES)
            raise ChecklistError(f"item {item_id}: status must be one of {allowed}")
        item: dict[str, Any] = {"id": item_id, "content": content, "status": status}
        active_form = entry.get("active_form")
        if isinstance(active_form, str) and active_form.strip():
            item["active_form"] = " ".join(active_form.split())[:120]
        reason = entry.get("blocked_reason")
        if status == "blocked":
            if not isinstance(reason, str) or not reason.strip():
                raise ChecklistError(f"item {item_id}: a blocked item needs blocked_reason")
            item["blocked_reason"] = " ".join(reason.split())[:200]
        items.append(item)
    warnings: list[str] = []
    if sum(1 for item in items if item["status"] == "in_progress") > 1:
        warnings.append("more than one item is in_progress; keep one at a time when you can")
    return items, warnings


class ChecklistService:
    def __init__(self, ctx) -> None:
        self._ctx = ctx
        self._lock = threading.Lock()
        # The turn each session is running now (set by the turn manager).
        self._current_turn: dict[str, str] = {}

    # -- reads -----------------------------------------------------------------

    def get(self, session_id: str) -> Checklist | None:
        row = self._ctx.db.query_one(
            "SELECT * FROM session_checklists WHERE session_id = ?", (session_id,)
        )
        return _from_row(row) if row is not None else None

    def visible(self, session_id: str) -> Checklist | None:
        """The list a client should show: none once it was cleared."""
        current = self.get(session_id)
        return None if current is None or current.state == "cleared" else current

    def render_for_prompt(self, session_id: str) -> str | None:
        """A list a previous turn left unfinished, for the model to reconcile."""
        current = self.get(session_id)
        if current is None or current.state not in ("open", "interrupted", "active"):
            return None
        if not current.items:
            return None
        marks = {"pending": " ", "in_progress": "~", "completed": "x", "blocked": "!"}
        lines = [
            "Checklist left from an earlier turn "
            + ("(interrupted)" if current.state == "interrupted" else "(unfinished)")
            + ": reconcile it with checklist.update before continuing (mark what is "
            "really done, update the rest), or clear it with items: [] if the user "
            "moved on."
        ]
        for item in current.items:
            line = f"- [{marks[item['status']]}] {item['id']}: {item['content']}"
            if item["status"] == "blocked":
                line += f" (blocked: {item.get('blocked_reason', '')})"
            lines.append(line)
        return "\n".join(lines)

    # -- writes ----------------------------------------------------------------

    def begin_turn(self, session_id: str, turn_id: str) -> Checklist | None:
        """A turn starts. A completed list is cleared; returns it if it changed."""
        with self._lock:
            self._current_turn[session_id] = turn_id
            current = self.get(session_id)
            if current is None or current.state != "completed":
                return None
            return self._write(session_id, current.turn_id, current, state="cleared")

    def end_turn(self, session_id: str) -> None:
        with self._lock:
            self._current_turn.pop(session_id, None)

    def replace(
        self, session_id: str, items: Any, explanation: Any = ""
    ) -> tuple[Checklist, list[str]]:
        normalized, warnings = validate_items(items)
        note = explanation if isinstance(explanation, str) else ""
        note = " ".join(note.split())[:300]
        with self._lock:
            current = self.get(session_id)
            turn_id = self._current_turn.get(session_id)
            state = "active" if normalized else "cleared"
            checklist = self._write(
                session_id,
                turn_id,
                current,
                state=state,
                items=normalized,
                explanation=note,
            )
        return checklist, warnings

    def settle(self, session_id: str, turn_id: str, outcome: str) -> Checklist | None:
        """End of a turn that worked on the list: the honest state it ends in.

        `outcome` is completed | failed | cancelled | stopped. Returns the
        list when it changed, None when this turn never touched it.
        """
        with self._lock:
            current = self.get(session_id)
            if current is None or current.state != "active" or current.turn_id != turn_id:
                return None
            left = any(item["status"] != "completed" for item in current.items)
            if not left:
                state = "completed"
            elif outcome == "completed":
                state = "open"
            else:
                state = "interrupted"
            return self._write(session_id, turn_id, current, state=state)

    def settle_orphans(self, turn_ids: list[str]) -> list[Checklist]:
        """Lists of turns a dead engine left behind: interrupted."""
        settled: list[Checklist] = []
        for turn_id in turn_ids:
            for row in self._ctx.db.query(
                "SELECT session_id FROM session_checklists WHERE turn_id = ? AND state = 'active'",
                (turn_id,),
            ):
                result = self.settle(str(row["session_id"]), turn_id, "interrupted")
                if result is not None:
                    settled.append(result)
        return settled

    def clear(self, session_id: str) -> Checklist | None:
        with self._lock:
            current = self.get(session_id)
            if current is None:
                return None
            return self._write(session_id, current.turn_id, current, state="cleared", items=[])

    def _write(
        self,
        session_id: str,
        turn_id: str | None,
        current: Checklist | None,
        *,
        state: str,
        items: list[dict[str, Any]] | None = None,
        explanation: str | None = None,
    ) -> Checklist:
        assert state in _STATES
        revision = (current.revision if current is not None else 0) + 1
        next_items = items if items is not None else list(current.items if current else ())
        note = explanation if explanation is not None else (current.explanation if current else "")
        now = now_iso(self._ctx.clock)
        self._ctx.db.execute(
            "INSERT INTO session_checklists "
            "(session_id, turn_id, revision, state, items_json, explanation, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET turn_id = excluded.turn_id, "
            "revision = excluded.revision, state = excluded.state, "
            "items_json = excluded.items_json, explanation = excluded.explanation, "
            "updated_at = excluded.updated_at",
            (
                session_id,
                turn_id,
                revision,
                state,
                json.dumps(next_items, ensure_ascii=False),
                note,
                now,
            ),
        )
        return Checklist(
            session_id=session_id,
            turn_id=turn_id,
            revision=revision,
            state=state,
            items=tuple(next_items),
            explanation=note,
            updated_at=now,
        )


def _from_row(row) -> Checklist:
    try:
        items = json.loads(row["items_json"] or "[]")
    except ValueError:
        items = []
    return Checklist(
        session_id=str(row["session_id"]),
        turn_id=row["turn_id"],
        revision=int(row["revision"]),
        state=str(row["state"]),
        items=tuple(item for item in items if isinstance(item, dict)),
        explanation=str(row["explanation"] or ""),
        updated_at=str(row["updated_at"]),
    )
