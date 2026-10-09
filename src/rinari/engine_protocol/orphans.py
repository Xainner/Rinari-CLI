"""Close turns an earlier engine process left open.

A turn is persisted as `turn.started` ... terminal event. When the engine
process exits mid-turn (a crash, a forced quit, the desktop killing it while a
blocking tool waits), the terminal event is never written: the timeline and
the `rinari.*` introspection show that turn as running forever, and its open
tool/model/agent rows keep spinning.

Reconciliation writes the terminal events the dead process could not. It
reuses the existing vocabulary (`turn.failed`, `tool.cancelled`,
`model.failed`, `agent.failed`, `vision.failed`) so every client already
renders it, and it is idempotent: a turn with a terminal event is never
touched again.

Ownership: one engine serves one Rinari home (a new engine supersedes the
previous one, as peers and questions already assume). A turn live in this
process is skipped, and so is a session whose cross-process turn lock is held
right now, so a turn executing elsewhere is never closed under it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from rinari.shared.clock import now_iso
from rinari.storage.records import SessionEventRecord

logger = logging.getLogger(__name__)

TERMINAL_TURN_EVENTS = ("turn.completed", "turn.failed", "turn.cancelled", "turn.stopped")
ENGINE_EXITED = "ENGINE_EXITED"
REASON = "engine_exited"
MESSAGE = (
    "Rinari's engine stopped before this turn finished, so it was closed as interrupted. "
    "What ran before that point is kept; you can continue or retry."
)

# (start events, terminal events, closing event, id field) per activity kind.
_ACTIVITIES = (
    (
        {"tool.requested", "tool.started"},
        {"tool.completed", "tool.failed", "tool.cancelled"},
        "tool.cancelled",
        "tool_call_id",
    ),
    ({"model.started"}, {"model.completed", "model.failed"}, "model.failed", "model_call_id"),
    ({"agent.started"}, {"agent.completed", "agent.failed"}, "agent.failed", "agent_id"),
    (
        {"vision.preparing", "vision.queued", "vision.started"},
        {"vision.completed", "vision.partial", "vision.failed", "vision.cancelled"},
        "vision.failed",
        "vision_id",
    ),
)


def find_orphans(db: Any, session_id: str | None = None) -> list[tuple[str, str]]:
    """`(session_id, turn_id)` of turns started without any terminal event."""
    marks = ", ".join("?" for _ in TERMINAL_TURN_EVENTS)
    sql = (
        "SELECT s.session_id, s.turn_id FROM session_events s "
        "WHERE s.type = 'turn.started' AND s.turn_id IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM session_events t WHERE t.session_id = s.session_id "
        f"AND t.turn_id = s.turn_id AND t.type IN ({marks}))"
    )
    params: list[Any] = list(TERMINAL_TURN_EVENTS)
    if session_id is not None:
        sql += " AND s.session_id = ?"
        params.append(session_id)
    sql += " ORDER BY s.session_id, s.seq"
    return [(str(row["session_id"]), str(row["turn_id"])) for row in db.query(sql, params)]


def _turn_lock_held(lock_dir: Path, session_id: str) -> bool:
    """True when another process holds this session's turn lock right now.

    A missing file means nobody ever ran a turn there from this home: probing
    must not create one (stale lock files are their own problem).
    """
    path = lock_dir / f"{session_id}.turn.lock"
    if not path.exists():
        return False
    from rinari.sessions.turn_lock import SessionTurnLock
    from rinari.shared.errors import ConflictError

    try:
        with SessionTurnLock(path, session_id):
            return False
    except ConflictError:
        return True
    except OSError:
        return True  # Unknown: leave the turn alone rather than guess.


def _open_activities(rows: Iterable[Any]) -> list[tuple[str, dict[str, Any]]]:
    """The activities this turn started and never closed, with their last payload."""
    found: list[tuple[str, dict[str, Any]]] = []
    for starts, ends, closing, key in _ACTIVITIES:
        opened: dict[str, dict[str, Any]] = {}
        closed: set[str] = set()
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            ident = payload.get(key)
            if not ident:
                continue
            if row.type in starts:
                opened[str(ident)] = {**opened.get(str(ident), {}), **payload}
            elif row.type in ends:
                closed.add(str(ident))
        for ident, payload in opened.items():
            if ident not in closed:
                found.append((closing, payload))
    return found


def _closing_payload(event_name: str, last: dict[str, Any]) -> dict[str, Any]:
    error = {"code": ENGINE_EXITED, "message": MESSAGE}
    if event_name == "tool.cancelled":
        return {
            "tool_call_id": last.get("tool_call_id"),
            "tool": last.get("tool", ""),
            "ok": False,
            "error": error,
        }
    if event_name == "model.failed":
        return {
            "model_call_id": last.get("model_call_id"),
            "model": last.get("model"),
            "error": {**error, "retryable": True},
        }
    if event_name == "agent.failed":
        return {
            "agent_id": last.get("agent_id"),
            "agent": last.get("agent"),
            "status": "cancelled",
            "error": MESSAGE,
        }
    # vision.failed keeps the identity fields its item is keyed and drawn by.
    keep = ("vision_id", "attempt_id", "route", "origin", "tool_call_id", "model_id")
    return {**{k: last[k] for k in keep if k in last}, "error": MESSAGE}


def _close_turn(ctx: Any, sid: str, turn_id: str) -> list[tuple[str, dict[str, Any]]]:
    rows = [
        row
        for row in ctx.event_repo.list(sid)
        if row.turn_id == turn_id and not row.type.startswith("usage.")
    ]
    if any(row.type in TERMINAL_TURN_EVENTS for row in rows):
        return []
    started = next((row for row in rows if row.type == "turn.started"), None)
    workspace = (started.payload or {}).get("workspace_root") if started else None
    next_seq = 1 + max((int(row.activity_seq or 0) for row in rows), default=0)
    occurred_at = now_iso(ctx.clock)
    failed = {
        "error": {
            "code": ENGINE_EXITED,
            "message": MESSAGE,
            # The work can be resumed by sending the request again.
            "retryable": True,
            "details": {"reason": REASON, "recoverable": True, "reconciled": True},
        }
    }
    writes = [
        *((name, _closing_payload(name, last)) for name, last in _open_activities(rows)),
        ("turn.failed", failed),
    ]
    records = []
    for offset, (name, body) in enumerate(writes):
        payload = {
            **body,
            "session_id": sid,
            "turn_id": turn_id,
            "activity_seq": next_seq + offset,
            "occurred_at": occurred_at,
        }
        if workspace:
            payload["workspace_root"] = workspace
        ctx.event_repo.insert(
            SessionEventRecord(
                id=ctx.ids.new("evt"),
                session_id=sid,
                seq=ctx.event_repo.next_seq(sid),
                type=name,
                payload=payload,
                created_at=occurred_at,
                turn_id=turn_id,
                activity_seq=int(payload["activity_seq"]),
            )
        )
        records.append((name, payload))
    return records


def reconcile(
    services: Any,
    *,
    is_live: Callable[[str], bool],
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    session_id: str | None = None,
) -> list[str]:
    """Close every orphaned turn (of one session, or of the whole home).

    Returns the reconciled turn ids. The caller serializes calls; each turn is
    re-checked right before writing so a repeated call writes nothing.
    """
    ctx = services.ctx
    lock_dir = ctx.layout.dir("sessions")
    reconciled: list[str] = []
    held: dict[str, bool] = {}
    for sid, turn_id in find_orphans(ctx.db, session_id):
        if is_live(turn_id):
            continue
        if sid not in held:
            held[sid] = _turn_lock_held(lock_dir, sid)
        if held[sid]:
            continue
        # BEGIN IMMEDIATE takes the write lock across processes: the re-check
        # and the writes are atomic, so a repeated call writes nothing.
        with ctx.db.transaction():
            records = _close_turn(ctx, sid, turn_id)
        if not records:
            continue
        reconciled.append(turn_id)
        logger.info(
            "closed orphaned turn %s of session %s (%s)",
            turn_id,
            sid,
            json.dumps([name for name, _ in records]),
        )
        if emit is not None:
            for name, payload in records:
                emit(name, payload)
    return reconciled


__all__ = ["ENGINE_EXITED", "find_orphans", "reconcile"]
