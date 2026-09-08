"""Turn execution bridge: worker threads, streaming events, approvals.

`session.turn.start` runs the real AgentLoop in a worker thread while the
stdio main loop keeps reading requests, so `session.turn.cancel` and
`approval.resolve` arrive mid-turn. Content/tool/approval events stream
through a thread-safe outbox the transport drains.

Turns are session-scoped: one active turn per session (TURN_RUNNING),
cancellation is session-scoped, and approval decisions map to engine
policy exactly (deny/allow_once/allow_session).
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rinari.application.services import ServiceContainer
from rinari.cli.agent_runtime import build_agent_session, run_turn
from rinari.engine_protocol import errors
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.messages import event
from rinari.policy.approvals import ApprovalRequest
from rinari.shared.errors import CancelledError, RinariError

APPROVAL_TIMEOUT_S = 600.0
APPROVAL_POLL_S = 0.05
MAX_DELTA_CHARS = 8000
MAX_DETAIL_CHARS = 2000
DECISIONS = ("deny", "allow_once", "allow_session")
_DECISION_TO_ANSWER = {"deny": "n", "allow_once": "y", "allow_session": "s"}


@dataclass
class _ActiveTurn:
    turn_id: str
    session_id: str
    session: Any
    done: threading.Event


@dataclass
class _PendingApproval:
    approval_id: str
    session_id: str | None
    turn_id: str | None
    decided: threading.Event = field(default_factory=threading.Event)
    decision: str | None = None


def _safe_detail(value: Any) -> Any:
    try:
        text = value if isinstance(value, str) else json.dumps(value)
        json.dumps(text)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > MAX_DETAIL_CHARS:
        return text[:MAX_DETAIL_CHARS] + "…[truncated]"
    return text


class TurnManager:
    """Owns turn worker threads, the event outbox, and pending approvals."""

    def __init__(self, services: ServiceContainer, user_home: Path | None = None) -> None:
        self._services = services
        self._user_home = user_home if user_home is not None else Path.home()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._turns: dict[str, _ActiveTurn] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._approvals: dict[str, _PendingApproval] = {}
        self._lock = threading.Lock()
        self._local = threading.local()

    # -- outbox ----------------------------------------------------------

    def drain_events(self) -> list[dict[str, Any]]:
        drained = []
        while True:
            try:
                drained.append(self._events.get_nowait())
            except queue.Empty:
                return drained

    def _emit(self, payload: dict[str, Any]) -> None:
        self._events.put(payload)

    # -- state ------------------------------------------------------------

    def has_active_turns(self) -> bool:
        with self._lock:
            return any(not turn.done.is_set() for turn in self._turns.values())

    # -- turn lifecycle ----------------------------------------------------

    def start_turn(self, session_id: str, message: str) -> dict[str, Any]:
        record = self._services.sessions.show(session_id)
        agent_session = build_agent_session(
            self._services,
            record,
            interactive=False,
            user_home=self._user_home,
            approval_prompt=self._prompt_for_current_turn,
        )
        turn_id = self._services.ctx.ids.new("turn")
        turn = _ActiveTurn(
            turn_id=turn_id,
            session_id=record.id,
            session=agent_session,
            done=threading.Event(),
        )
        with self._lock:
            for active in self._turns.values():
                if active.session_id == record.id and not active.done.is_set():
                    agent_session.end()
                    raise EngineProtocolError(
                        errors.TURN_RUNNING,
                        f"Session {record.id} already has a running turn.",
                        details={"turn_id": active.turn_id, "session_id": record.id},
                    )
            self._turns[turn_id] = turn
        self._emit(event("turn.started", {"turn_id": turn_id, "session_id": record.id}))
        thread = threading.Thread(target=self._run_turn, args=(turn_id, message), daemon=True)
        with self._lock:
            self._threads[turn_id] = thread
        thread.start()
        return {"status": "started", "turn_id": turn_id, "session_id": record.id}

    def cancel_turn(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            turn = next(
                (
                    active
                    for active in self._turns.values()
                    if active.session_id == session_id and not active.done.is_set()
                ),
                None,
            )
        if turn is None:
            raise EngineProtocolError(
                errors.NO_ACTIVE_TURN, f"Session {session_id} has no running turn."
            )
        turn.session.token.cancel()
        return {
            "status": "cancel_requested",
            "turn_id": turn.turn_id,
            "session_id": session_id,
        }

    def cancel_all_turns(self) -> None:
        with self._lock:
            active = [turn for turn in self._turns.values() if not turn.done.is_set()]
        for turn in active:
            with contextlib.suppress(Exception):
                turn.session.token.cancel()

    def close(self) -> None:
        self.cancel_all_turns()
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=10)

    def _run_turn(self, turn_id: str, message: str) -> None:
        turn = self._turns[turn_id]
        self._local.turn_id = turn_id
        self._local.token = turn.session.token
        try:
            result = run_turn(
                turn.session,
                message,
                on_delta=self._delta_cb(turn),
                on_tool=self._tool_cb(turn),
            )
            if result.kind == "cancelled":
                self._emit(
                    event("turn.cancelled", {"turn_id": turn_id, "session_id": turn.session_id})
                )
            else:
                self._emit(
                    event(
                        "turn.completed",
                        {
                            "turn_id": turn_id,
                            "session_id": turn.session_id,
                            "kind": result.kind,
                            "content": result.content,
                        },
                    )
                )
        except RinariError as err:
            mapped = errors.from_rinari_error(err)
            self._emit(
                event(
                    "turn.failed",
                    {
                        "turn_id": turn_id,
                        "session_id": turn.session_id,
                        "error": {
                            "code": mapped.code,
                            "message": mapped.message,
                            "retryable": mapped.retryable,
                            "details": mapped.details,
                        },
                    },
                )
            )
        except Exception as err:  # defensive: the desktop must see a terminal event
            self._emit(
                event(
                    "turn.failed",
                    {
                        "turn_id": turn_id,
                        "session_id": turn.session_id,
                        "error": {
                            "code": errors.ENGINE_ERROR,
                            "message": f"{type(err).__name__}: {err}",
                            "retryable": False,
                            "details": {},
                        },
                    },
                )
            )
        finally:
            turn.done.set()
            turn.session.end()
            self._local.turn_id = None
            self._local.token = None

    # -- streaming callbacks (worker thread) --------------------------------

    def _delta_cb(self, turn: _ActiveTurn) -> Any:
        def _on_delta(text: str) -> None:
            if len(text) > MAX_DELTA_CHARS:
                text = text[:MAX_DELTA_CHARS] + "…[truncated]"
            self._emit(
                event(
                    "model.content.delta",
                    {"turn_id": turn.turn_id, "session_id": turn.session_id, "delta": text},
                )
            )

        return _on_delta

    def _tool_cb(self, turn: _ActiveTurn) -> Any:
        def _on_tool(phase: str, name: str, detail: Any) -> None:
            self._emit(
                event(
                    "tool.started" if phase == "start" else "tool.completed",
                    {
                        "turn_id": turn.turn_id,
                        "session_id": turn.session_id,
                        "tool": name,
                        "detail": _safe_detail(detail),
                    },
                )
            )

        return _on_tool

    # -- approvals (worker thread blocks, main loop resolves) -----------------

    def _prompt_for_current_turn(self, request: ApprovalRequest) -> str:
        return self._answer_approval(request, getattr(self._local, "token", None))

    def _answer_approval(self, request: ApprovalRequest, token: Any | None = None) -> str:
        if token is not None and getattr(token, "cancelled", False):
            raise CancelledError("Approval abandoned: turn was cancelled.")
        turn_id = getattr(self._local, "turn_id", None)
        approval_id = self._services.ctx.ids.new("apr")
        pending = _PendingApproval(
            approval_id=approval_id, session_id=request.session_id, turn_id=turn_id
        )
        with self._lock:
            self._approvals[approval_id] = pending
        self._emit(
            event(
                "approval.requested",
                {
                    "approval_id": approval_id,
                    "session_id": request.session_id,
                    "turn_id": turn_id,
                    "tool": request.capability,
                    "capability": request.capability,
                    "target": request.target,
                    "risk": request.risk,
                    "description": request.description,
                    "choices": list(DECISIONS),
                },
            )
        )
        deadline = time.time() + APPROVAL_TIMEOUT_S
        try:
            while not pending.decided.wait(APPROVAL_POLL_S):
                if token is not None and getattr(token, "cancelled", False):
                    raise CancelledError("Approval abandoned: turn was cancelled.")
                if time.time() >= deadline:
                    self._emit(
                        event(
                            "approval.expired",
                            {"approval_id": approval_id, "session_id": request.session_id},
                        )
                    )
                    return "n"
        finally:
            with self._lock:
                self._approvals.pop(approval_id, None)
        if pending.decision is None:
            return "n"
        self._emit(
            event(
                "approval.resolved",
                {
                    "approval_id": approval_id,
                    "session_id": request.session_id,
                    "decision": pending.decision,
                },
            )
        )
        return _DECISION_TO_ANSWER[pending.decision]

    def resolve_approval(self, approval_id: str, decision: str) -> dict[str, Any]:
        if decision not in DECISIONS:
            raise EngineProtocolError(
                errors.INVALID_PARAMS,
                f"Param 'decision' must be one of {', '.join(DECISIONS)}.",
            )
        with self._lock:
            pending = self._approvals.get(approval_id)
            if pending is None:
                raise EngineProtocolError(
                    errors.APPROVAL_NOT_FOUND, f"Unknown or expired approval: {approval_id}."
                )
            pending.decision = decision
            pending.decided.set()
        return {"status": "resolved", "approval_id": approval_id, "decision": decision}
