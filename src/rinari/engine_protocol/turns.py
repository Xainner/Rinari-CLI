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

import collections
import contextlib
import json
import queue
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from rinari.application.services import ServiceContainer
from rinari.application.session_service import (
    SESSION_STATE_ARCHIVED,
    SESSION_STATE_CLOSED,
    profile_for_session,
)
from rinari.cli.agent_runtime import build_agent_session, run_turn
from rinari.engine_protocol import errors
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.messages import event
from rinari.policy.approvals import ApprovalRequest
from rinari.shared.clock import now_iso
from rinari.shared.errors import CancelledError, RinariError
from rinari.storage.records import SessionEventRecord

APPROVAL_TIMEOUT_S = 600.0
APPROVAL_POLL_S = 0.05
PREPARATION_TIMEOUT_S = 20.0
PREPARATION_POLL_S = 0.05
MAX_DELTA_CHARS = 8000
MAX_DETAIL_CHARS = 2000
MAX_QUEUE_DEPTH = 20
MAX_QUEUE_MESSAGE_CHARS = 32768
DECISIONS = ("deny", "allow_once", "allow_session")
_DECISION_TO_ANSWER = {"deny": "n", "allow_once": "y", "allow_session": "s"}


@dataclass
class _ActiveTurn:
    turn_id: str
    session_id: str
    session: Any | None
    done: threading.Event
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.time)
    status: str = "running"
    mode: str | None = None
    preparation_stage: str | None = None
    activities: dict[str, dict[str, Any]] = field(default_factory=dict)
    activity_keys: dict[str, int] = field(default_factory=dict)
    next_activity_seq: int = 1
    governor: dict[str, Any] = field(
        default_factory=lambda: {
            "execution": "automatic",
            "status": "healthy",
            "recovery_attempts": 0,
            "max_recovery_attempts": 3,
            "compactions": 0,
            "context_pressure": None,
        }
    )


@dataclass
class _PendingApproval:
    approval_id: str
    session_id: str | None
    turn_id: str | None
    decided: threading.Event = field(default_factory=threading.Event)
    decision: str | None = None
    capability: str = ""
    target: str | None = None
    risk: str = "medium"
    description: str = ""
    choices: tuple[str, ...] = DECISIONS
    rule_id: str = "default"
    reusable: bool = True


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
        self._preparation_threads: set[threading.Thread] = set()
        self._approvals: dict[str, _PendingApproval] = {}
        self._closed_approvals: dict[str, str] = {}
        self._queue: dict[str, collections.deque[str]] = {}
        self._lock = threading.RLock()
        self._local = threading.local()
        from rinari.engine_protocol.questions import QuestionBroker

        self.questions = QuestionBroker(services, self.has_active_turn)

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

    def emit_external(self, payload: dict[str, Any]) -> None:
        """Server-side events (e.g. mode changes) on the same ordered queue."""
        self._events.put(payload)

    # -- state ------------------------------------------------------------

    def has_active_turns(self) -> bool:
        with self._lock:
            return any(not turn.done.is_set() for turn in self._turns.values())

    def has_active_turn(self, session_id: str) -> bool:
        with self._lock:
            return any(
                not turn.done.is_set() and turn.session_id == session_id
                for turn in self._turns.values()
            )

    def conflicts_with_changeset(self, changeset: dict[str, Any]) -> bool:
        """Return whether undo would race an active turn in its session/project."""
        with self._lock:
            active_session_ids = {
                turn.session_id for turn in self._turns.values() if not turn.done.is_set()
            }
        if changeset["session_id"] in active_session_ids:
            return True
        project_id = changeset.get("project_id")
        if not project_id:
            return False
        return any(
            self._services.sessions.show(session_id).project_id == project_id
            for session_id in active_session_ids
        )

    def emit_persisted_activity(
        self,
        event_name: str,
        *,
        session_id: str,
        turn_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Append post-turn lifecycle activity, such as a conservative undo."""
        rows = self._services.ctx.event_repo.list(session_id)
        activity_seq = 1 + max(
            (int(row.activity_seq or 0) for row in rows if row.turn_id == turn_id),
            default=0,
        )
        occurred_at = now_iso(self._services.ctx.clock)
        safe = {
            "session_id": session_id,
            "turn_id": turn_id,
            "activity_seq": activity_seq,
            "occurred_at": occurred_at,
            **payload,
        }
        self._persist_activity(event_name, safe)
        self._emit(event(event_name, safe))

    def runtime_state(self) -> dict[str, Any]:
        """Presentation-safe live state used to recover after a UI reload."""
        with self._lock:
            turns = [
                {
                    "turn_id": turn.turn_id,
                    "session_id": turn.session_id,
                    "status": "cancelling" if turn.cancel_requested.is_set() else turn.status,
                    "preparation_stage": turn.preparation_stage,
                    "mode": turn.mode,
                    "started_at": turn.started_at,
                    "items": sorted(
                        (dict(item) for item in turn.activities.values()),
                        key=lambda item: int(item.get("activity_seq") or 0),
                    ),
                    "activities": sorted(
                        (dict(item) for item in turn.activities.values()),
                        key=lambda item: int(item.get("activity_seq") or 0),
                    ),
                    "next_activity_seq": turn.next_activity_seq,
                    "governor": dict(turn.governor),
                }
                for turn in self._turns.values()
                if not turn.done.is_set()
            ]
            approvals = [
                {
                    "approval_id": item.approval_id,
                    "session_id": item.session_id,
                    "turn_id": item.turn_id,
                    "capability": item.capability,
                    "target": item.target,
                    "risk": item.risk,
                    "description": item.description,
                    "choices": list(item.choices),
                    "rule_id": item.rule_id,
                    "reusable": item.reusable,
                }
                for item in self._approvals.values()
            ]
        return {"active_turns": turns, "pending_approvals": approvals}

    # -- turn lifecycle ----------------------------------------------------

    def start_turn(
        self, session_id: str, message: str, reasoning_effort: str | None = None
    ) -> dict[str, Any]:
        record = self._services.sessions.show(session_id)
        if record.state in {SESSION_STATE_CLOSED, SESSION_STATE_ARCHIVED}:
            raise EngineProtocolError(
                errors.SESSION_CLOSED,
                f"Session {record.id} is closed; resume it before starting turns.",
                details={"session_id": record.id},
            )
        turn_id = self._services.ctx.ids.new("turn")
        turn = _ActiveTurn(
            turn_id=turn_id,
            session_id=record.id,
            session=None,
            done=threading.Event(),
        )
        with self._lock:
            record = self._services.sessions.show(session_id)
            for active in self._turns.values():
                if active.session_id == record.id and not active.done.is_set():
                    raise EngineProtocolError(
                        errors.TURN_RUNNING,
                        f"Session {record.id} already has a running turn.",
                        details={"turn_id": active.turn_id, "session_id": record.id},
                    )
            record = self._services.sessions.name_from_first_message(record.id, message)
            self._turns[turn_id] = turn
            turn.mode = record.mode
        self._activity_cb(turn)(
            "turn.started",
            {
                "reasoning_effort": reasoning_effort,
                "mode": record.mode,
                "message": message,
            },
        )
        thread = threading.Thread(
            target=self._run_turn,
            args=(turn_id, record, message, reasoning_effort),
            daemon=True,
        )
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
        turn.cancel_requested.set()
        turn.status = "cancelling"
        if turn.session is not None:
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
                turn.cancel_requested.set()
                if turn.session is not None:
                    turn.session.token.cancel()

    def close(self) -> None:
        self.cancel_all_turns()
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=10)
        # A timed-out/cancelled turn can leave its bounded runtime builder
        # finishing in the background. Keep the service container alive until
        # those builders have observed abandonment and released DB resources.
        with self._lock:
            preparation_threads = list(self._preparation_threads)
        for thread in preparation_threads:
            thread.join(timeout=PREPARATION_TIMEOUT_S + 1)

    def _run_turn(
        self,
        turn_id: str,
        record: Any,
        message: str,
        reasoning_effort: str | None,
    ) -> None:
        turn = self._turns[turn_id]
        self._local.turn_id = turn_id
        tracker = None
        changes_finalized = False

        def finalize_changes() -> None:
            nonlocal changes_finalized
            if changes_finalized or tracker is None:
                return
            changes_finalized = True
            changeset = tracker.finalize()
            if changeset is not None:
                self._activity_cb(turn)("turn.changes.completed", changeset)

        try:
            agent_session = self._prepare_session(turn, record, reasoning_effort)
            tracker = self._services.changes.begin(
                self._services,
                record,
                turn_id,
                worktree=agent_session.context.tool_ctx.worktree,
            )
            agent_session.context.tool_ctx = replace(
                agent_session.context.tool_ctx,
                change_tracker=tracker,
            )
            with self._lock:
                turn.session = agent_session
            self._local.token = agent_session.token
            if turn.cancel_requested.is_set():
                agent_session.token.cancel()
            self._activity_cb(turn)("turn.preparing", {"stage": "session_lock"})
            result = run_turn(
                agent_session,
                message,
                turn_id=turn_id,
            )
            if result.kind == "cancelled":
                self._cancel_running_activities(turn)
                finalize_changes()
                self._activity_cb(turn)("turn.cancelled", {})
            elif result.stop_reason is not None:
                finalize_changes()
                self._activity_cb(turn)(
                    "turn.stopped",
                    {
                        "reason": result.stop_reason,
                        "recoverable": result.recoverable,
                        "details": {
                            "content": result.content,
                            "governor": result.governor,
                        },
                        "usage": result.budget,
                    },
                )
            else:
                finalize_changes()
                self._activity_cb(turn)(
                    "turn.completed",
                    {"kind": result.kind, "content": result.content},
                )
        except CancelledError:
            self._cancel_running_activities(turn)
            finalize_changes()
            self._activity_cb(turn)("turn.cancelled", {})
        except EngineProtocolError as err:
            self._fail_running_activities(turn, err.message)
            finalize_changes()
            self._activity_cb(turn)(
                "turn.failed",
                {
                    "error": {
                        "code": err.code,
                        "message": err.message,
                        "retryable": err.retryable,
                        "details": err.details,
                    },
                },
            )
        except RinariError as err:
            self._fail_running_activities(turn, str(err))
            finalize_changes()
            mapped = errors.from_rinari_error(err)
            self._activity_cb(turn)(
                "turn.failed",
                {
                    "error": {
                        "code": mapped.code,
                        "message": mapped.message,
                        "retryable": mapped.retryable,
                        "details": mapped.details,
                    },
                },
            )
        except Exception as err:  # defensive: the desktop must see a terminal event
            self._fail_running_activities(turn, str(err))
            finalize_changes()
            self._activity_cb(turn)(
                "turn.failed",
                {
                    "error": {
                        "code": errors.ENGINE_ERROR,
                        "message": f"{type(err).__name__}: {err}",
                        "retryable": False,
                        "details": {},
                    },
                },
            )
        finally:
            turn.done.set()
            if turn.session is not None:
                turn.session.end()
            self._local.turn_id = None
            self._local.token = None
            self._start_next_queued(turn.session_id)

    def _prepare_session(self, turn: _ActiveTurn, record: Any, reasoning_effort: str | None) -> Any:
        """Build the per-turn runtime without stranding cancellation or the UI.

        Most preparation is local and normally completes in under a second.
        Extension discovery, keyring backends, or filesystem edge cases can
        nevertheless block. Run it behind a bounded handoff so Stop remains
        responsive and a broken initializer becomes a terminal turn failure.
        """
        completed: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        abandoned = threading.Event()

        def prepare() -> None:
            session = None
            try:
                session = build_agent_session(
                    self._services,
                    record,
                    interactive=False,
                    user_home=self._user_home,
                    profile=profile_for_session(record),
                    approval_prompt=self._prompt_for_current_turn,
                    question_prompt=lambda args, token, deadline: self.questions.ask(
                        turn, self._activity_cb(turn), args, token, deadline
                    ),
                    activity_sink=self._activity_cb(turn),
                    reasoning_effort=reasoning_effort,
                )
                if abandoned.is_set():
                    session.end()
                    return
                completed.put_nowait((True, session))
            except BaseException as exc:
                if abandoned.is_set():
                    if session is not None:
                        with contextlib.suppress(Exception):
                            session.end()
                    return
                with contextlib.suppress(queue.Full):
                    completed.put_nowait((False, exc))

        def tracked_prepare() -> None:
            try:
                prepare()
            finally:
                with self._lock:
                    self._preparation_threads.discard(threading.current_thread())

        thread = threading.Thread(
            target=tracked_prepare,
            name="rinari-turn-prepare",
            daemon=True,
        )
        with self._lock:
            self._preparation_threads.add(thread)
        thread.start()
        deadline = time.monotonic() + PREPARATION_TIMEOUT_S
        while True:
            if turn.cancel_requested.is_set():
                abandoned.set()
                raise CancelledError("Turn cancelled during runtime preparation")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                abandoned.set()
                stage = turn.preparation_stage or "runtime"
                raise EngineProtocolError(
                    errors.TURN_PREPARATION_TIMEOUT,
                    (
                        "Turn runtime preparation stalled during "
                        f"{stage!r} for more than {PREPARATION_TIMEOUT_S:g} seconds."
                    ),
                    retryable=True,
                    details={"stage": stage, "timeout_s": PREPARATION_TIMEOUT_S},
                )
            try:
                ok, value = completed.get(timeout=min(PREPARATION_POLL_S, remaining))
            except queue.Empty:
                continue
            if ok:
                return value
            raise value

    # -- prompt queue ------------------------------------------------------
    # Queued messages run FIFO after the live turn ends, preserving normal
    # turn boundaries and approvals. Bounded; engine-process memory only.

    def queue_add(self, session_id: str, message: str) -> dict[str, Any]:
        record = self._services.sessions.show(session_id)
        text = (message or "").strip()
        if not text:
            raise EngineProtocolError(errors.INVALID_PARAMS, "Queued message is empty.")
        if len(text) > MAX_QUEUE_MESSAGE_CHARS:
            raise EngineProtocolError(
                errors.INVALID_PARAMS,
                f"Queued message exceeds {MAX_QUEUE_MESSAGE_CHARS} chars.",
            )
        with self._lock:
            pending = self._queue.setdefault(record.id, collections.deque())
            if len(pending) >= MAX_QUEUE_DEPTH:
                raise EngineProtocolError(
                    errors.INVALID_PARAMS,
                    f"Queue full ({MAX_QUEUE_DEPTH} pending).",
                )
            pending.append(text)
            position = len(pending)
        self._emit(
            event(
                "session.queue.updated",
                {"session_id": record.id, "pending": position},
            )
        )
        return {"session_id": record.id, "position": position, "pending": position}

    def queue_list(self, session_id: str) -> dict[str, Any]:
        record = self._services.sessions.show(session_id)
        with self._lock:
            pending = list(self._queue.get(record.id, ()))
        return {"session_id": record.id, "queue": pending, "pending": len(pending)}

    def queue_clear(self, session_id: str) -> dict[str, Any]:
        record = self._services.sessions.show(session_id)
        with self._lock:
            removed = len(self._queue.pop(record.id, ()))
        self._emit(event("session.queue.updated", {"session_id": record.id, "pending": 0}))
        return {"session_id": record.id, "removed": removed}

    def _start_next_queued(self, session_id: str) -> None:
        with self._lock:
            pending = self._queue.get(session_id)
            if not pending:
                return
            if any(
                not turn.done.is_set() and turn.session_id == session_id
                for turn in self._turns.values()
            ):
                return
            message = pending.popleft()
            remaining = len(pending)
            if not pending:
                self._queue.pop(session_id, None)
        self._emit(
            event(
                "session.queue.updated",
                {"session_id": session_id, "pending": remaining},
            )
        )
        try:
            self.start_turn(session_id, message)
        except EngineProtocolError:
            with self._lock:
                self._queue.setdefault(session_id, collections.deque()).appendleft(message)

    # -- streaming callbacks (worker thread) --------------------------------

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

    def _activity_cb(self, turn: _ActiveTurn) -> Any:
        def _on_activity(event_name: str, payload: dict[str, Any]) -> None:
            if turn.done.is_set():
                return
            safe = {
                "turn_id": turn.turn_id,
                "session_id": turn.session_id,
                **payload,
            }
            safe["workspace_root"] = self._services.sessions.show(turn.session_id).current_cwd
            arguments = safe.get("arguments")
            if (
                safe.get("tool") in {"fs.write", "fs.patch"}
                and isinstance(arguments, dict)
                and isinstance(arguments.get("path"), str)
            ):
                safe["file_path"] = arguments["path"]
            if "arguments" in safe:
                safe["arguments"] = _safe_detail(safe["arguments"])
            if "result" in safe:
                safe["result"] = _safe_detail(safe["result"])
            occurred_at = now_iso(self._services.ctx.clock)
            safe["occurred_at"] = occurred_at
            activity_key = self._activity_key(turn, event_name, safe)
            with self._lock:
                activity_seq = turn.activity_keys.get(activity_key)
                if activity_seq is None:
                    activity_seq = turn.next_activity_seq
                    turn.next_activity_seq += 1
                    turn.activity_keys[activity_key] = activity_seq
                safe["activity_seq"] = activity_seq
                if event_name == "turn.preparing":
                    turn.preparation_stage = str(safe.get("stage") or "") or None
                if event_name.startswith("governor."):
                    snapshot = safe.get("governor")
                    if isinstance(snapshot, dict):
                        turn.governor = {**turn.governor, **snapshot, "event": event_name}
                    else:
                        turn.governor = {**turn.governor, **safe, "event": event_name}
                current = turn.activities.get(activity_key, {})
                if event_name == "model.content.delta":
                    delta = str(safe.get("delta") or "")
                    safe["delta"] = delta[:MAX_DELTA_CHARS]
                    current = {**current, "content": str(current.get("content") or "") + delta}
                elif event_name == "model.content.completed":
                    current = {**current, "content": str(safe.get("content") or "")}
                turn.activities[activity_key] = {**current, **safe, "event": event_name}
            if event_name != "model.content.delta":
                self._persist_activity(event_name, safe)
            self._emit(event(event_name, safe))

        return _on_activity

    def _activity_key(self, turn: _ActiveTurn, event_name: str, payload: dict[str, Any]) -> str:
        if payload.get("tool_call_id"):
            return f"tool:{payload['tool_call_id']}"
        if payload.get("model_call_id"):
            return f"model:{payload['model_call_id']}"
        if payload.get("request_id") and event_name.startswith("question."):
            return f"question:{payload['request_id']}"
        if payload.get("approval_id"):
            return f"approval:{payload['approval_id']}"
        if event_name.startswith("turn.changes."):
            change_id = payload.get("id") or payload.get("changeset_id") or turn.turn_id
            return f"changeset:{change_id}"
        if event_name == "governor.compact":
            return f"context:compact:{payload.get('history_size', 0)}"
        if event_name.startswith("verification."):
            return "verification:completion-gate"
        if event_name.startswith("agent.") and payload.get("agent_id"):
            phase = "terminal" if event_name in {"agent.completed", "agent.failed"} else "started"
            return f"agent:{payload['agent_id']}:{phase}"
        if event_name == "turn.preparing":
            return "system:preparing"
        if event_name.startswith("turn."):
            return f"system:{event_name}"
        return f"event:{self._services.ctx.ids.new('activity')}:{event_name}"

    def _persist_activity(self, event_name: str, payload: dict[str, Any]) -> None:
        with (
            contextlib.nullcontext()
            if event_name.startswith("question.")
            else contextlib.suppress(Exception)
        ):
            self._services.ctx.event_repo.insert(
                SessionEventRecord(
                    id=self._services.ctx.ids.new("evt"),
                    session_id=str(payload["session_id"]),
                    seq=self._services.ctx.event_repo.next_seq(str(payload["session_id"])),
                    type=event_name,
                    payload=dict(payload),
                    created_at=str(payload["occurred_at"]),
                    turn_id=str(payload["turn_id"]),
                    activity_seq=int(payload["activity_seq"]),
                )
            )

    def _cancel_running_activities(self, turn: _ActiveTurn) -> None:
        with self._lock:
            running = [
                dict(item)
                for item in turn.activities.values()
                if item.get("event") == "tool.started"
            ]
        for item in running:
            self._activity_cb(turn)(
                "tool.cancelled",
                {
                    "tool_call_id": item.get("tool_call_id"),
                    "tool": item.get("tool", ""),
                    "ok": False,
                    "error": {"code": "cancelled", "message": "Turn cancelled"},
                },
            )

    def _fail_running_activities(self, turn: _ActiveTurn, message: str) -> None:
        with self._lock:
            running = [
                dict(item)
                for item in turn.activities.values()
                if item.get("event") == "tool.started"
            ]
        for item in running:
            self._activity_cb(turn)(
                "tool.failed",
                {
                    "tool_call_id": item.get("tool_call_id"),
                    "tool": item.get("tool", ""),
                    "ok": False,
                    "error": {"code": "turn_failed", "message": message},
                },
            )

    # -- approvals (worker thread blocks, main loop resolves) -----------------

    def _prompt_for_current_turn(self, request: ApprovalRequest) -> str:
        return self._answer_approval(request, getattr(self._local, "token", None))

    def _answer_approval(self, request: ApprovalRequest, token: Any | None = None) -> str:
        if token is not None and getattr(token, "cancelled", False):
            raise CancelledError("Approval abandoned: turn was cancelled.")
        turn_id = getattr(self._local, "turn_id", None)
        approval_id = self._services.ctx.ids.new("apr")
        pending = _PendingApproval(
            approval_id=approval_id,
            session_id=request.session_id,
            turn_id=turn_id,
            capability=request.capability,
            target=request.target,
            risk=request.risk,
            description=request.description,
            choices=request.choices,
            rule_id=request.rule_id,
            reusable=request.reusable,
        )
        with self._lock:
            self._approvals[approval_id] = pending
        turn = self._turns.get(str(turn_id))
        approval_payload = {
            "approval_id": approval_id,
            "tool": request.capability,
            "capability": request.capability,
            "target": request.target,
            "risk": request.risk,
            "description": request.description,
            "choices": list(request.choices),
            "rule_id": request.rule_id,
            "reusable": request.reusable,
        }
        if turn is not None:
            self._activity_cb(turn)("approval.requested", approval_payload)
        else:
            self._emit(
                event(
                    "approval.requested",
                    {
                        **approval_payload,
                        "session_id": request.session_id,
                        "turn_id": turn_id,
                    },
                )
            )
        deadline = time.time() + APPROVAL_TIMEOUT_S
        expired = False
        try:
            while not pending.decided.wait(APPROVAL_POLL_S):
                if token is not None and getattr(token, "cancelled", False):
                    raise CancelledError("Approval abandoned: turn was cancelled.")
                if time.time() >= deadline:
                    expired = True
                    if turn is not None:
                        self._activity_cb(turn)(
                            "approval.expired",
                            {"approval_id": approval_id, "reason": "timeout"},
                        )
                    else:
                        self._emit(
                            event(
                                "approval.expired",
                                {
                                    "approval_id": approval_id,
                                    "session_id": request.session_id,
                                    "turn_id": turn_id,
                                    "reason": "timeout",
                                },
                            )
                        )
                    return "n"
        except CancelledError:
            expired = True
            if turn is not None:
                self._activity_cb(turn)(
                    "approval.expired",
                    {"approval_id": approval_id, "reason": "turn_cancelled"},
                )
            else:
                self._emit(
                    event(
                        "approval.expired",
                        {
                            "approval_id": approval_id,
                            "session_id": request.session_id,
                            "turn_id": turn_id,
                            "reason": "turn_cancelled",
                        },
                    )
                )
            raise
        finally:
            with self._lock:
                self._approvals.pop(approval_id, None)
                self._closed_approvals[approval_id] = "expired" if expired else "resolved"
                if len(self._closed_approvals) > 200:
                    self._closed_approvals.pop(next(iter(self._closed_approvals)))
        if pending.decision is None:
            return "n"
        if turn is not None:
            self._activity_cb(turn)(
                "approval.resolved",
                {"approval_id": approval_id, "decision": pending.decision},
            )
        else:
            self._emit(
                event(
                    "approval.resolved",
                    {
                        "approval_id": approval_id,
                        "session_id": request.session_id,
                        "turn_id": turn_id,
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
                status = self._closed_approvals.get(approval_id)
                if status is not None:
                    return {"status": status, "approval_id": approval_id, "decision": decision}
                raise EngineProtocolError(
                    errors.APPROVAL_NOT_FOUND, f"Unknown approval: {approval_id}."
                )
            if decision not in pending.choices:
                raise EngineProtocolError(
                    errors.INVALID_PARAMS,
                    f"Decision {decision!r} is not available for this approval.",
                )
            pending.decision = decision
            pending.decided.set()
        return {"status": "resolved", "approval_id": approval_id, "decision": decision}
