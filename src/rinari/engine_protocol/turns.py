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
import logging
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
from rinari.engine_protocol.operations import OperationStore
from rinari.engine_protocol.token_usage import TurnTokenTracker
from rinari.models.types import ChatMessage
from rinari.policy.approvals import ApprovalRequest
from rinari.policy.engine import PermissionProfile
from rinari.shared.clock import now_iso
from rinari.shared.errors import CancelledError, RinariError
from rinari.shared.redaction import redact_value
from rinari.storage.records import SessionEventRecord

logger = logging.getLogger(__name__)

APPROVAL_TIMEOUT_S = 600.0
APPROVAL_POLL_S = 0.05
PREPARATION_TIMEOUT_S = 20.0
PREPARATION_POLL_S = 0.05
MAX_DELTA_CHARS = 8000
MAX_DETAIL_CHARS = 2000
MAX_STREAM_CHARS = 64_000
_TOOL_ENDS = frozenset({"tool.completed", "tool.failed", "tool.cancelled"})


def _origin_kind(origin: dict[str, Any] | None) -> str:
    """Who a turn's message comes from, for the tools that only trust the owner.

    A child's brief and its mother's messages are shown as the owner's
    (`kind: user`), but the model wrote them: they never count as the owner
    asking (a rule, a quote to keep, a memory).
    """
    origin = origin or {}
    kind = origin.get("kind") or "user"
    if kind == "user" and origin.get("source") in ("child_brief", "parent_message"):
        return "child_brief"
    return kind


MAX_QUEUE_DEPTH = 20
MAX_QUEUE_MESSAGE_CHARS = 32768
# What the model reads before a message the owner sent mid-turn. The chat
# shows only the owner's text (display content).
STEER_NOW_PREFIX = (
    "[The user interrupted your current step to tell you this. Read it now; it may "
    "change or refine the current task. Anything you were writing was cut off.]\n\n"
)
STEER_PREFIX = (
    "[The user sent this while you were working. Take it into account from here on; "
    "it may change or refine the current task.]\n\n"
)
DECISIONS = ("deny", "allow_once", "allow_session", "allow_project")
_DECISION_TO_ANSWER = {"deny": "n", "allow_once": "y", "allow_session": "s", "allow_project": "p"}


@dataclass
class _QueuedPrompt:
    """A manual queue entry (legacy `session.queue.add`), origin user.

    A scheduled task that runs in its conversation waits here too, with its
    own origin and `memory_origin`: it is not the owner speaking.
    """

    message: str
    origin: dict[str, Any] | None = None
    display_message: str | None = None
    memory_origin: str = "interactive"


@dataclass
class _ActiveTurn:
    turn_id: str
    session_id: str
    session: Any | None
    done: threading.Event
    # Procedencia del mensaje que inició el turno; `None` = petición del
    # propietario. Un origen `peer` limita el runtime (techo de procedencia)
    # y alimenta la cadena/hop de cualquier envío posterior.
    origin: dict[str, Any] | None = None
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.time)
    status: str = "running"
    mode: str | None = None
    remote_target: dict | None = None
    channel: dict | None = None
    memory_origin: str = "automation"
    operation_id: str = ""
    attachments: list | None = None
    display_message: str | None = None
    attachment_metadata: list | None = None
    allow_unconfirmed_vision: bool = False
    compaction_only: bool = False
    # Compact, then run the turn's message: "Compact and continue" after a
    # turn stopped by a failed automatic compaction.
    continue_after: bool = False
    # Slash command that started the turn (`learn`…); becomes turn_command.
    command: str = ""
    # Paths the turn changed, for a child's report to its mother.
    changed_paths: list[str] = field(default_factory=list)
    # Checklist the turn starts with: the steps of the plan it implements (#55).
    seed_checklist: list[dict[str, Any]] | None = None
    # `/init` (#81): this turn alone is read-only, whatever the session's
    # mode, and starts with these tools already loaded.
    read_only: bool = False
    activate_tools: tuple[str, ...] = ()
    # The process registry whose exits refresh this turn's context (#90).
    process_watch: Any | None = None
    activity_lock: Any = field(default_factory=threading.RLock)
    # The first visible text of the turn decides `model.changed`, once.
    model_notice_checked: bool = False
    terminal_emitted: bool = False
    preparation_stage: str | None = None
    activities: dict[str, dict[str, Any]] = field(default_factory=dict)
    activity_keys: dict[str, int] = field(default_factory=dict)
    next_activity_seq: int = 1
    token_usage: TurnTokenTracker = field(default_factory=TurnTokenTracker)
    # Steering: messages the owner sent while the turn runs, waiting for the
    # loop to pick them up after the current step. Closed (under the
    # manager lock) once the turn is finishing; later messages queue.
    steering: list[ChatMessage] = field(default_factory=list)
    steering_open: bool = True
    steer_count: int = 0
    terminal_event: str | None = None
    # What the goal runner needs from a finished turn (plan 0.4.0 #23).
    goal_result: Any = None
    goal_error: tuple[str, str] = ("", "")
    files_changed: int = 0
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


# Events whose text is the owner's, not the model's or a tool's.
_OWNER_TEXT_EVENTS = ("steer.", "question.")


def _safe_detail(value: Any) -> Any:
    try:
        text = value if isinstance(value, str) else json.dumps(value)
        json.dumps(text)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > MAX_DETAIL_CHARS:
        return text[:MAX_DETAIL_CHARS] + "…[truncated]"
    return text


def _safe_structured(value: Any, *, depth: int = 0, budget: int = MAX_DETAIL_CHARS) -> Any:
    """Bound structured presentation data while retaining its JSON shape."""

    if depth > 5:
        return "[nested data truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= budget else value[:budget] + "…[truncated]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 64:
                result["[additional fields]"] = "[truncated]"
                break
            result[str(key)] = _safe_structured(item, depth=depth + 1, budget=budget)
        try:
            if len(json.dumps(result, ensure_ascii=False, default=str)) > budget:
                result["[presentation] truncated"] = True
                while (
                    len(json.dumps(result, ensure_ascii=False, default=str)) > budget
                    and len(result) > 1
                ):
                    result.pop(next(iter(result)))
        except (TypeError, ValueError):
            return _safe_detail(value)
        return result
    if isinstance(value, (list, tuple)):
        result = [_safe_structured(item, depth=depth + 1, budget=budget) for item in value[:64]]
        if len(value) > 64:
            result.append("[additional items truncated]")
        return result
    return _safe_detail(value)


def _safe_presentation(value: dict[str, Any], max_stream_chars: int = 64_000) -> dict[str, Any]:
    """Bound UI-facing tool data without converting it into model JSON."""

    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"stdout", "stderr"} and isinstance(item, str):
            result[key] = item[:max_stream_chars]
            if len(item) > max_stream_chars:
                result["truncated"] = True
        elif key == "data" or (key == "error" and isinstance(item, dict)):
            result[key] = _safe_structured(item)
        elif key == "artifacts" and isinstance(item, list):
            result[key] = [str(uri) for uri in item[:32]]
        else:
            result[key] = item
    return result


class TurnManager:
    """Owns turn worker threads, the event outbox, and pending approvals."""

    def __init__(self, services: ServiceContainer, user_home: Path | None = None) -> None:
        self._services = services
        self.operations = OperationStore(services.ctx.layout.root)
        self._user_home = user_home if user_home is not None else Path.home()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._turns: dict[str, _ActiveTurn] = {}
        self._desktop_browsers: dict[str, Any] = {}
        # Registry de contextos nativos (documento 03 §4.1). La pone el
        # servidor al construirse; sin ella el comportamiento es el de siempre.
        self._browser_registry: Any | None = None
        self._desktop_processes: dict[str, Any] = {}
        # #90: the durable list of the processes the model starts.
        self._process_ledger: Any | None = None
        with contextlib.suppress(Exception):
            from rinari.engine_protocol.process_ledger import ProcessLedger

            self._process_ledger = ProcessLedger(services.ctx.layout.root)
        self._threads: dict[str, threading.Thread] = {}
        self._preparation_threads: set[threading.Thread] = set()
        self._approvals: dict[str, _PendingApproval] = {}
        self._observers: list[Any] = []
        # session_id -> whether its last turn read something from outside the
        # machine (the Agency passes that on to the turn it wakes).
        self._last_external: dict[str, bool] = {}
        # A resume waiting for a provider limit holds the session's queue
        # (plan 0.4.0 #24); set by the resume runner.
        self.queue_hold: Any = None
        # Told what «Vaciar» took out of a session's queue: a scheduled run
        # waiting there ends cancelled, set by the schedule runner.
        self.queue_cleared: Any = None
        self._closed_approvals: dict[str, str] = {}
        self._queue: dict[str, collections.deque[_QueuedPrompt]] = {}
        self._lock = threading.RLock()
        self._local = threading.local()
        from rinari.engine_protocol.questions import QuestionBroker

        self.questions = QuestionBroker(services, self.has_active_turn)
        from rinari.engine_protocol.channels import ChannelBroker

        self.channels = ChannelBroker()
        # «In this chat» / «in this session» grants, one list per conversation (#56).
        from rinari.policy.session_grants import SessionGrantRegistry

        self.grants = SessionGrantRegistry(services.ctx.db)
        # Child sessions a conversation opens for a separate piece of work.
        from rinari.engine_protocol.children import ChildSessions

        self.children = ChildSessions(self)
        # Nor leaves its turns open: one that died mid-turn is closed now.
        self.reconcile_orphan_turns()
        try:
            # Cheap housekeeping: lock files of sessions deleted long ago.
            services.sessions.sweep_turn_locks()
        except Exception:
            logger.exception("turn lock sweep failed")

    def set_browser_registry(self, registry: Any) -> None:
        """Registry de contextos de browser nativo, puesta por el servidor."""
        self._browser_registry = registry

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
        self._notify(payload)

    def emit_external(self, payload: dict[str, Any]) -> None:
        """Server-side events (e.g. mode changes) on the same ordered queue."""
        self._events.put(payload)
        self._notify(payload)

    def add_observer(self, observer: Any) -> None:
        """Engine-side listeners (the scheduler follows its runs this way).

        Called on the emitting thread, after the event is queued; a failing
        observer never breaks the turn."""
        self._observers.append(observer)

    def _notify(self, payload: dict[str, Any]) -> None:
        for observer in list(self._observers):
            try:
                observer(payload)
            except Exception:
                logger.exception("turn event observer failed")

    # -- state ------------------------------------------------------------

    def has_active_turns(self) -> bool:
        with self._lock:
            return any(not turn.done.is_set() for turn in self._turns.values())

    def last_turn_external(self, session_id: str) -> bool:
        """Whether the conversation's last finished turn read external content."""
        with self._lock:
            return self._last_external.get(session_id, False)

    def _note_external(self, turn) -> None:
        """Remember whether this turn read external content, before its terminal event.

        Also when it failed or was cancelled: never the previous turn's value
        (#75). Before the turn had a session, its origin's mark is all there is.
        """
        state = None
        if turn.session is not None:
            state = getattr(getattr(turn.session.context, "tool_ctx", None), "turn_state", None)
        if state is not None:
            external = bool(getattr(state, "external_content", False))
        else:
            external = bool((turn.origin or {}).get("external_content"))
        with self._lock:
            self._last_external[turn.session_id] = external

    def has_active_turn(self, session_id: str) -> bool:
        with self._lock:
            return any(
                not turn.done.is_set() and turn.session_id == session_id
                for turn in self._turns.values()
            )

    def reconcile_orphan_turns(self, session_id: str | None = None) -> list[str]:
        """Close turns a dead engine process left without a terminal event.

        Best effort: a failure here must never keep the engine from starting
        or a session from opening.
        """
        from rinari.engine_protocol.orphans import reconcile

        def is_live(turn_id: str) -> bool:
            with self._lock:
                turn = self._turns.get(turn_id)
                return turn is not None and not turn.done.is_set()

        try:
            with self._lock:
                reconciled = reconcile(
                    self._services,
                    is_live=is_live,
                    emit=lambda name, payload: self._emit(event(name, payload)),
                    session_id=session_id,
                )
        except Exception:
            logger.exception("orphaned turn reconciliation failed")
            return []
        self._checklist_orphans(reconciled)
        return reconciled

    # -- live checklist ---------------------------------------------------------

    def _checklist_service(self) -> Any:
        return getattr(self._services, "checklist", None)

    def _checklist_begin(self, turn: _ActiveTurn) -> None:
        followups = getattr(self._services, "followups", None)
        if followups is not None:
            with contextlib.suppress(Exception):
                followups.begin_turn(turn.session_id, turn.turn_id)
        service = self._checklist_service()
        if service is None:
            return
        try:
            cleared = service.begin_turn(turn.session_id, turn.turn_id)
        except Exception:
            logger.exception("checklist rollover failed")
            return
        if cleared is not None:
            # Cleared (the last turn finished it) or active again (#66b).
            reason = "rollover" if cleared.state == "cleared" else "resumed"
            self._activity_cb(turn)(
                "checklist.updated", {"reason": reason, "checklist": cleared.as_dict()}
            )
        if turn.seed_checklist:
            # Implementing a plan: its steps are the turn's list (plan 0.4.0 #55).
            try:
                seeded, _ = service.replace(turn.session_id, turn.seed_checklist)
            except Exception:
                logger.exception("plan checklist seed failed")
                return
            self._activity_cb(turn)(
                "checklist.updated", {"reason": "plan", "checklist": seeded.as_dict()}
            )

    def _checklist_end(self, turn: _ActiveTurn) -> None:
        followups = getattr(self._services, "followups", None)
        if followups is not None:
            with contextlib.suppress(Exception):
                followups.end_turn(turn.session_id)
        service = self._checklist_service()
        if service is not None:
            with contextlib.suppress(Exception):
                service.end_turn(turn.session_id)

    def _checklist_settle(self, turn: _ActiveTurn, terminal: str, emit: Any) -> None:
        service = self._checklist_service()
        if service is None:
            return
        try:
            settled = service.settle(turn.session_id, turn.turn_id, terminal.removeprefix("turn."))
        except Exception:
            logger.exception("checklist settle failed")
            return
        if settled is not None:
            emit(
                "checklist.updated",
                {"reason": terminal.replace(".", "_"), "checklist": settled.as_dict()},
            )

    def _checklist_orphans(self, turn_ids: list[str]) -> None:
        service = self._checklist_service()
        if service is None or not turn_ids:
            return
        try:
            settled = service.settle_orphans(turn_ids)
        except Exception:
            logger.exception("checklist orphan settle failed")
            return
        for checklist in settled:
            with contextlib.suppress(Exception):
                self.emit_persisted_activity(
                    "checklist.updated",
                    session_id=checklist.session_id,
                    turn_id=str(checklist.turn_id),
                    payload={"reason": "engine_restart", "checklist": checklist.as_dict()},
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

    def emit_followup_resolution(self, suggestion: Any) -> None:
        """`followup.resolved` on the turn that left the note (live or after)."""
        payload = {
            "suggestion_id": suggestion.id,
            "status": suggestion.status,
            "suggestion": suggestion.as_dict(),
        }
        if not suggestion.turn_id:
            self._emit(event("followup.resolved", {**payload, "session_id": suggestion.session_id}))
            return
        with self._lock:
            live = self._turns.get(suggestion.turn_id)
        if live is not None and not live.done.is_set():
            self._activity_cb(live)("followup.resolved", payload)
        else:
            self.emit_persisted_activity(
                "followup.resolved",
                session_id=suggestion.session_id,
                turn_id=suggestion.turn_id,
                payload=payload,
            )

    def emit_memory_resolution(self, candidate: dict[str, Any]) -> bool:
        """`memory.candidate.resolved` on the turn that showed the proposal.

        A live turn takes it as ordinary activity (the card updates in place);
        a finished one gets it appended, so a reloaded timeline shows the card
        as resolved. Candidates no turn announced (message-derived ones) emit
        nothing: the desktop refetches the list. Returns whether it emitted.
        """
        session_id = str(candidate.get("session_id") or "")
        turn_id = None
        for row in self._services.ctx.db.query(
            "SELECT turn_id, payload_json FROM session_events WHERE session_id = ? "
            "AND type = 'memory.candidate.created' AND turn_id IS NOT NULL ORDER BY seq DESC",
            (session_id,),
        ):
            if _json_payload(row["payload_json"]).get("candidate_id") == candidate.get("id"):
                turn_id = str(row["turn_id"])
                break
        if turn_id is None:
            return False
        status = {"accepted": "approved", "denied": "denied"}.get(str(candidate.get("status")))
        if status is None:
            return False
        payload: dict[str, Any] = {"candidate_id": candidate["id"], "status": status}
        if candidate.get("memory_id"):
            payload["memory_id"] = candidate["memory_id"]
        with self._lock:
            live = self._turns.get(turn_id)
        if live is not None and not live.done.is_set():
            self._activity_cb(live)("memory.candidate.resolved", payload)
        else:
            self.emit_persisted_activity(
                "memory.candidate.resolved", session_id=session_id, turn_id=turn_id, payload=payload
            )
        return True

    def emit_skill_resolution(
        self, name: str, status: str, extra: dict[str, Any] | None = None
    ) -> bool:
        """`skill.proposal.resolved` on the turn whose card showed `name`.

        Same rule as memory: a live turn takes it as activity, a finished
        one gets it appended so a reloaded chat shows the card resolved.
        """
        found = None
        for row in self._services.ctx.db.query(
            "SELECT session_id, turn_id, payload_json FROM session_events "
            "WHERE type = 'skill.proposed' AND turn_id IS NOT NULL ORDER BY seq DESC",
            (),
        ):
            if _json_payload(row["payload_json"]).get("name") == name:
                found = (str(row["session_id"]), str(row["turn_id"]))
                break
        if found is None:
            return False
        session_id, turn_id = found
        payload: dict[str, Any] = {"name": name, "status": status, **(extra or {})}
        with self._lock:
            live = self._turns.get(turn_id)
        if live is not None and not live.done.is_set():
            self._activity_cb(live)("skill.proposal.resolved", payload)
        else:
            self.emit_persisted_activity(
                "skill.proposal.resolved",
                session_id=session_id,
                turn_id=turn_id,
                payload=payload,
            )
        return True

    def start_init_turn(self, session_id: str) -> dict[str, Any]:
        """`/init` (#81): explore the project read-only and propose its RINARI.md."""
        from rinari.tools.native.instructions import (
            INSTRUCTIONS_FILE,
            REUSABLE,
            current_instructions,
        )

        record = self._services.sessions.show(session_id)
        root = Path(record.project_root_snapshot) if record.project_root_snapshot else None
        if record.kind != "PROJECT" or root is None or not root.is_dir():
            raise EngineProtocolError(
                errors.INVALID_PARAMS,
                "/init needs a conversation in a project: RINARI.md belongs to a project folder.",
                details={"session_id": session_id},
            )
        existing = current_instructions(root)
        reusable = [name for name in REUSABLE if (root / name).is_file()]
        lines = [
            "[/init] Get to know this project and propose its RINARI.md.",
            "This turn is read-only: explore with reads, searches and listings; do not write, "
            "install or run anything that changes the project.",
            "Find out what the project is and its stack; how it is installed, tested, built and "
            "run (the real commands from its manifests, scripts and CI); its conventions, "
            "important folders and what must not be touched.",
            "Then call project.instructions.propose once with the whole file. Put the commands "
            "under a «## Cómo probar» (or «## How to test») section, one per line in backticks. "
            "Write it in the user's language and keep it short and factual.",
        ]
        if existing is not None:
            lines.append(
                f"The project already has a {INSTRUCTIONS_FILE}: propose the updated whole file, "
                "keeping what is still true. The user sees the difference before saving."
            )
        if reusable:
            lines.append(
                "It also has " + " and ".join(reusable) + ": reuse them (summarize what matters or "
                "point to them) instead of duplicating, and list them in reuses."
            )
        lines.append("Finish with a two-line summary of what you proposed.")
        return self.start_turn(
            session_id,
            "\n".join(lines),
            display_message="/init",
            origin={"kind": "user", "command": "init", "command_kind": "ui"},
            command="init",
            read_only=True,
            activate_tools=("project.instructions.propose",),
        )

    def instructions_proposal(self, session_id: str, proposal_id: str) -> dict[str, Any] | None:
        """The `instructions.proposed` event of a /init card, with its turn."""
        for row in self._services.ctx.db.query(
            "SELECT turn_id, payload_json FROM session_events "
            "WHERE session_id = ? AND type = 'instructions.proposed' ORDER BY seq DESC",
            (session_id,),
        ):
            payload = _json_payload(row["payload_json"])
            if payload.get("proposal_id") == proposal_id:
                return {**payload, "turn_id": row["turn_id"]}
        return None

    def emit_instructions_resolution(
        self, session_id: str, turn_id: str | None, payload: dict[str, Any]
    ) -> None:
        """`instructions.proposal.resolved` on the turn whose card showed it."""
        with self._lock:
            live = self._turns.get(turn_id or "")
        if live is not None and not live.done.is_set():
            self._activity_cb(live)("instructions.proposal.resolved", payload)
        elif turn_id:
            self.emit_persisted_activity(
                "instructions.proposal.resolved",
                session_id=session_id,
                turn_id=turn_id,
                payload=payload,
            )

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

    def _warn_pasted_secret(self, session_id: str, message: str) -> None:
        """Rinari Agency (D-11): the owner pasted what looks like a key in an agent's chat.

        It already went to the model's provider with the message, so it cannot
        be taken back: the desktop says so and recommends changing it. The
        event never carries the text, only that it happened.
        """
        from rinari.memory.service import find_sensitive_match
        from rinari.shared.redaction import redact_text

        text = message or ""
        if find_sensitive_match(text) is None and redact_text(text) == text:
            return
        self.emit_external(event("agency.secret_pasted", {"session_id": session_id}))

    def _check_agent(self, record: Any) -> None:
        """Rinari Agency: an agent's turn waits for the owner past its daily cap.

        Checked before the turn exists, so nothing is spent. An unknown cost
        (a model without a known price) never stops it: only a token cap can.
        """
        from rinari.engine_protocol.agency import spend_today

        agency = getattr(self._services, "agency", None)
        if agency is None:
            return
        agent = agency.for_session(record.id)
        if agent is None:
            raise EngineProtocolError(
                errors.AGENT_ARCHIVED,
                "This agent was archived; its conversation is read-only.",
                details={"session_id": record.id, "agent_id": record.agent_id},
            )
        spend = spend_today(self._services, agent)
        if spend["over_budget"]:
            raise EngineProtocolError(
                errors.AGENT_OVER_BUDGET,
                f"{agent['name']} used today's budget; raise it or wait until tomorrow.",
                details={"session_id": record.id, "agent_id": agent["id"], "spend": spend},
            )
        # A2: a post also stops at its team's cap, and every agent at the Agency's.
        from rinari.engine_protocol.agency import agency_settings, spend_of

        team_id = getattr(record, "team_id", None)
        team = self._services.ctx.team_repo.get(team_id) if team_id else None
        if team is not None and team.get("budget"):
            team_spend = spend_of(self._services, "team", team["id"], team["budget"])
            if team_spend["over_budget"]:
                raise EngineProtocolError(
                    errors.AGENT_OVER_BUDGET,
                    f"The team {team['name']} used today's budget.",
                    details={"session_id": record.id, "team_id": team["id"], "spend": team_spend},
                )
        global_budget = agency_settings(self._services)["budget"]
        if global_budget:
            total = spend_of(self._services, "agency", None, global_budget)
            if total["over_budget"]:
                raise EngineProtocolError(
                    errors.AGENT_OVER_BUDGET,
                    "The Agency used today's budget.",
                    details={"session_id": record.id, "spend": total},
                )

    def start_turn(
        self,
        session_id: str,
        message: str,
        reasoning_effort: str | None = None,
        *,
        turn_id: str | None = None,
        remote_target: dict | None = None,
        channel: dict | None = None,
        operation_id: str = "",
        attachments: list | None = None,
        display_message: str | None = None,
        attachment_metadata: list | None = None,
        allow_unconfirmed_vision: bool = False,
        memory_origin: str = "interactive",
        compaction_only: bool = False,
        origin: dict[str, Any] | None = None,
        command: str = "",
        continue_after: bool = False,
        seed_checklist: list[dict[str, Any]] | None = None,
        read_only: bool = False,
        activate_tools: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        record = self._services.sessions.show(session_id)
        # A scheduled task that runs in its conversation («Repetir aquí») runs
        # there even when it was archived, and leaves it archived.
        scheduled_here = (
            record.state == SESSION_STATE_ARCHIVED
            and memory_origin == "automation"
            and (origin or {}).get("kind") == "schedule"
        )
        if record.state in {SESSION_STATE_CLOSED, SESSION_STATE_ARCHIVED} and not scheduled_here:
            raise EngineProtocolError(
                errors.SESSION_CLOSED,
                f"Session {record.id} is closed; resume it before starting turns.",
                details={"session_id": record.id},
            )
        if record.agent_id and not compaction_only:
            self._check_agent(record)
            if reasoning_effort is None:
                # Rinari Agency: the agent's effort, for its chat, its posts,
                # its rules and its children, unless the turn chose one.
                agent = self._services.ctx.agency_repo.get(record.agent_id) or {}
                reasoning_effort = agent.get("reasoning_effort") or None
            if origin is None and memory_origin == "interactive":
                self._warn_pasted_secret(record.id, message)
        # A turn is what recency means, and nothing else refreshed it here, so
        # a session stayed where it was however much was said in it. Compaction
        # is housekeeping the user did not ask for and must not reorder a list.
        # Runs on the caller's thread, before any worker exists.
        if not compaction_only or continue_after:
            record = self._services.sessions.touch(record.id)
        turn_id = turn_id or self._services.ctx.ids.new("turn")
        turn = _ActiveTurn(
            turn_id=turn_id,
            session_id=record.id,
            session=None,
            done=threading.Event(),
            remote_target=remote_target,
            channel=channel,
            memory_origin=memory_origin,
            operation_id=operation_id,
            attachments=attachments,
            origin=origin,
            display_message=display_message,
            attachment_metadata=attachment_metadata,
            allow_unconfirmed_vision=allow_unconfirmed_vision,
            compaction_only=compaction_only,
            continue_after=continue_after and compaction_only,
            command=command,
            seed_checklist=seed_checklist,
            read_only=read_only,
            activate_tools=tuple(activate_tools),
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
            self._turns[turn_id] = turn
            turn.mode = record.mode
        self._activity_cb(turn)(
            "turn.started",
            {
                "reasoning_effort": reasoning_effort,
                "mode": record.mode,
                "message": display_message or message,
                "attachments": attachment_metadata or [],
                **({"origin": origin} if origin else {}),
            },
        )
        self.children.before_turn(turn)
        thread = threading.Thread(
            target=self._run_turn,
            args=(turn_id, record, message, reasoning_effort),
            daemon=True,
        )
        with self._lock:
            self._threads[turn_id] = thread
        thread.start()
        return {"status": "started", "turn_id": turn_id, "session_id": record.id}

    def start_operation(
        self,
        operation_id: str,
        session_id: str,
        message: str,
        reasoning_effort: str | None = None,
        remote_target: dict | None = None,
        channel: dict | None = None,
        attachments: list | None = None,
        *,
        attachment_metadata: list | None = None,
        display_message: str | None = None,
        allow_unconfirmed_vision: bool = False,
        memory_origin: str = "automation",
    ) -> dict[str, Any]:
        if memory_origin not in {"interactive", "owner_channel", "automation"}:
            raise EngineProtocolError(
                errors.INVALID_PARAMS,
                "Invalid memory origin",
            )
        with self._lock:
            turn_id = self._services.ctx.ids.new("turn")
            operation, claimed = self.operations.claim(
                operation_id,
                session_id,
                message,
                reasoning_effort,
                turn_id,
                remote_target,
                channel,
                attachment_metadata or attachments,
                memory_origin=memory_origin,
            )
            if claimed:
                try:
                    self.start_turn(
                        session_id,
                        message,
                        reasoning_effort,
                        turn_id=turn_id,
                        remote_target=remote_target,
                        channel=channel,
                        operation_id=operation_id,
                        attachments=attachments,
                        attachment_metadata=attachment_metadata,
                        display_message=display_message,
                        allow_unconfirmed_vision=allow_unconfirmed_vision,
                        memory_origin=memory_origin,
                    )
                except Exception:
                    # Dispatch may have reached a worker: never label this retryable.
                    self.operations.finish(turn_id, "uncertain")
                    raise
            return self.operations.get(operation_id) or operation

    def cancel_operation(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            operation = self.operations.get(operation_id)
            if operation is None:
                raise EngineProtocolError(errors.INVALID_PARAMS, "Unknown operation")
            turn = self._turns.get(operation["turn_id"])
            if turn is not None and not turn.done.is_set():
                turn.cancel_requested.set()
                turn.status = "cancelling"
                if turn.session is not None:
                    turn.session.token.cancel()
                self.operations.finish(turn.turn_id, "cancelling")
            return self.operations.get(operation_id) or operation

    def cancel_turn(self, session_id: str, turn_id: str | None = None) -> dict[str, Any]:
        """Cancel the session's running turn; with `turn_id`, only if it is that one."""
        with self._lock:
            turn = next(
                (
                    active
                    for active in self._turns.values()
                    if active.session_id == session_id
                    and not active.done.is_set()
                    and (turn_id is None or active.turn_id == turn_id)
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
        with self._lock:
            browsers = list(self._desktop_browsers.values())
            self._desktop_browsers.clear()
            registry = self._browser_registry
        for session_id in list(self._desktop_processes):
            self.close_processes(session_id)
        for browser in browsers:
            with contextlib.suppress(Exception):
                browser.close()
        if registry is not None:
            with contextlib.suppress(Exception):
                registry.release_all()

    def close_processes(self, session_id: str) -> None:
        with self._lock:
            registry = self._desktop_processes.pop(session_id, None)
        if registry is not None:
            for handle in registry.list():
                if handle.process.poll() is None:
                    registry.kill(handle)
                    registry.wait(handle, 2)

    def close_browser(self, session_id: str) -> None:
        with self._lock:
            browser = self._desktop_browsers.pop(session_id, None)
            registry = self._browser_registry
        if browser is not None:
            browser.close()
        # La registry es la autoridad de ownership del contexto nativo. Sin
        # esto, cerrar la sesión retiraba el manager de esta caché pero lo
        # dejaba en la otra, y un `acquire` posterior devolvía uno ya cerrado.
        if registry is not None:
            registry.release(session_id)

    def browser_view(self, params: dict[str, Any]) -> dict[str, Any]:
        """Observe the exact CDP page used by the engine; never launch or navigate."""
        session_id = str(params.get("session_id") or "")
        self._services.sessions.show(session_id)
        with self._lock:
            browser = self._desktop_browsers.get(session_id)
        if browser is None:
            return {"state": "disconnected", "session_id": session_id}

        # Con browser nativo esto **no** consulta al host. Este handler corre
        # en el loop de stdio, que despacha en serie, y la respuesta del host
        # sólo puede entrar por ese mismo loop: pedirle algo aquí es un
        # bloqueo permanente, no una espera lenta. El §5.4 lo dice al revés
        # —lo que espera al host va en workers—, y esta es la cara del Engine
        # de esa regla.
        #
        # Además no haría falta: el §10 conserva `browser.view.get` como
        # visor de capturas del backend externo, y con vista nativa la UI
        # muestra la superficie real, no una imagen de ella.
        if getattr(browser, "_backend", None) is not None:
            # Cada turno prepara el navegador de su sesión por si lo usa, así
            # que tenerlo no significa usarlo: «conectado» exige además una
            # página abierta. Sin esto, cualquier conversación que hubiera
            # corrido un turno encendía el botón Navegador «en vivo».
            pages = (
                self._browser_registry is not None
                and self._browser_registry.describe(session_id).get("context_state") == "ready"
            )
            return {
                "state": "connected" if browser.connected and pages else "disconnected",
                "session_id": session_id,
                "backend": "electron-native",
                "native_surface": True,
            }

        try:
            status = browser.status()
            result = {**status, "session_id": session_id, "instance": str(browser.profile_dir)}
            if not browser.connected:
                return result
            targets = browser.targets()
            result["pages"] = targets
            target_id = str(params.get("target_id") or "")
            target = next((t for t in targets if t["target_id"] == target_id), None)
            target = (
                target
                or next((t for t in targets if t["url"] != "about:blank"), None)
                or (targets[0] if targets else None)
            )
            if target:
                result.update(target)
                capture = browser.call(
                    target["target_id"],
                    "Page.captureScreenshot",
                    {
                        "format": "jpeg",
                        "quality": 60,
                        "captureBeyondViewport": False,
                    },
                    domain="Page",
                    timeout_s=3,
                )
                data = capture.get("data", "")
                if len(data) <= 2_000_000:
                    result["image"] = "data:image/jpeg;base64," + data
                else:
                    result["error"] = "Browser image exceeds the 2 MB preview limit."
            return result
        except Exception as exc:
            return {
                "state": "connected" if browser.connected else "disconnected",
                "session_id": session_id,
                "error": str(exc),
            }

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
                turn.files_changed = len(changeset.get("files") or [])
                turn.changed_paths = [
                    str(row.get("path"))
                    for row in changeset.get("files") or []
                    if isinstance(row, dict) and row.get("path")
                ]
                self._activity_cb(turn)("turn.changes.completed", changeset)

        trusted_before = self._trusted_folders(record)
        try:
            self._checklist_begin(turn)
            agent_session = self._prepare_session(turn, record, reasoning_effort)
            context = agent_session.context
            if turn.activate_tools:
                # Announced with their exact call when outside the fixed list
                # (F42); the list itself does not change.
                agent_session.next_turn_tools = tuple(turn.activate_tools)
            with self._lock:
                processes = self._desktop_processes.setdefault(
                    turn.session_id, context.tool_ctx.processes
                )
                context.tool_ctx = replace(context.tool_ctx, processes=processes)
                browser = self._desktop_browsers.get(turn.session_id)
                if browser is None and self._browser_registry is not None:
                    # El browser del escritorio tiene preferencia cuando existe:
                    # es la página que el usuario está viendo. Una vez elegido
                    # se queda cacheado, para que la sesión no cambie de
                    # backend entre turnos (§1).
                    browser = self._browser_registry.acquire(turn.session_id)
                    if browser is not None:
                        self._desktop_browsers[turn.session_id] = browser
                if browser is None:
                    browser = getattr(context.tool_ctx, "browser", None)
                    if browser is not None:
                        self._desktop_browsers[turn.session_id] = browser
                if browser is not None:
                    context.tool_ctx = replace(context.tool_ctx, browser=browser)
            self._watch_processes(turn, context, processes)
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
            if turn.attachments:
                from rinari.models.images import references

                turn.session.context.pending_images = references(
                    self._services.artifacts, turn.session_id, turn.attachments
                )
            turn.session.context.pending_attachments = tuple(turn.attachment_metadata or ())
            turn.session.context.pending_display_content = turn.display_message
            turn.session.context.pending_origin = dict(turn.origin) if turn.origin else None
            if not turn.compaction_only or turn.continue_after:
                turn.session.context.collect_steering = lambda: self._drain_steering(turn)
            if turn.compaction_only:
                from rinari.cli.agent_runtime import compact_session
                from rinari.runtime.agent import TurnResult

                compact_session(agent_session, self._activity_cb(turn))
                if turn.continue_after and message:
                    # A failed compaction raised above and nothing continues.
                    result = run_turn(
                        agent_session,
                        message,
                        turn_id=turn_id,
                        memory_origin=turn.memory_origin,
                    )
                else:
                    result = TurnResult(kind="compaction", content="", tool_calls=0, usage=None)
            else:
                result = run_turn(
                    agent_session,
                    message,
                    turn_id=turn_id,
                    memory_origin=turn.memory_origin,
                )
            turn.goal_result = result
            self._note_external(turn)
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
                            "stop": result.stop_detail,
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
            self._note_external(turn)
            self._cancel_running_activities(turn)
            finalize_changes()
            self._activity_cb(turn)("turn.cancelled", {})
        except EngineProtocolError as err:
            self._note_external(turn)
            turn.goal_error = (err.code, err.message)
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
            self._note_external(turn)
            self._fail_running_activities(turn, str(err))
            finalize_changes()
            mapped = errors.from_rinari_error(err)
            turn.goal_error = (
                str(getattr(err, "error_code", "") or mapped.code),
                mapped.message,
            )
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
            self._note_external(turn)
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
            with self._lock:
                unfinished = [
                    dict(item)
                    for key, item in turn.activities.items()
                    if item.get("event") == "agent.started"
                    and f"agent:{item.get('agent_id')}:terminal" not in turn.activities
                ]
            for item in unfinished:
                self._activity_cb(turn)(
                    "agent.failed",
                    {
                        "agent_id": item["agent_id"],
                        "agent": item.get("agent"),
                        "status": "cancelled",
                        "error": "Parent turn ended before the agent returned a result.",
                    },
                )
            leftover = self._close_steering(turn)
            turn.done.set()
            if turn.session is not None:
                turn.session.context.collect_steering = None
                # Browser belongs to the desktop session, not this individual turn.
                turn.session.context.tool_ctx = replace(turn.session.context.tool_ctx, browser=None)
                turn.session.end()
            self._local.turn_id = None
            self._local.token = None
            self._settle_steering(turn, leftover)
            self._checklist_end(turn)
            watched = getattr(turn, "process_watch", None)
            if watched is not None:
                watched.on_exit = None
            self._trust_after_turn(record, trusted_before)
            # The child's report first: a queued or goal turn that starts next
            # must not be overwritten as finished (#100).
            self.children.report_turn(turn)
            self._start_next_queued(turn.session_id)
            self._goal_after_turn(turn)
            self.children.after_turn(turn)

    def _watch_processes(self, turn: _ActiveTurn, context: Any, processes: Any) -> None:
        """Tell the model what it has running, and what ends during the turn (#90)."""
        from rinari.tools.native.process import ProcessRegistry, processes_note

        if not isinstance(processes, ProcessRegistry):
            return
        if processes.ledger is None and self._process_ledger is not None:
            processes.ledger = self._process_ledger
            processes.session_id = turn.session_id
        base_state = context.assembler_base.task_state
        ended: list[Any] = []

        def refresh() -> None:
            ended.extend(processes.take_exit_notices())
            note = processes_note(processes, ended)
            state = "\n\n".join(part for part in (base_state, note) if part) or None
            context.assembler_base = replace(context.assembler_base, task_state=state)

        refresh()
        processes.on_exit = refresh
        turn.process_watch = processes

    def _trusted_folders(self, record: Any) -> dict[str, str]:
        """The project's folders that were trusted when the turn started (#38)."""
        if getattr(record, "kind", None) != "PROJECT" or not getattr(record, "project_id", None):
            return {}
        try:
            folders = self._services.projects.folders.list(record.project_id)
        except Exception:
            return {}
        trusted: dict[str, str] = {}
        for folder in folders:
            path = Path(folder["path"])
            if not path.is_dir():
                continue
            with contextlib.suppress(Exception):
                state = self._services.trust.status(path).state
                if state == "trusted":
                    trusted[str(path)] = state
        return trusted

    def _trust_after_turn(self, record: Any, trusted_before: dict[str, str]) -> None:
        """Tell the desktop now when the turn changed a folder's identity (#38).

        It used to find out only when the conversation was opened again. A
        `git init` without remotes keeps the trust on its own and is reported
        as information; any other change asks for trust again right away.
        """
        for raw, previous in trusted_before.items():
            try:
                status = self._services.trust.status(Path(raw))
            except Exception:
                continue
            if status.state == previous and status.reidentified is None:
                continue
            self._emit(
                event(
                    "project.trust.changed",
                    {
                        "session_id": record.id,
                        "project_id": record.project_id,
                        "path": raw,
                        "state": status.state,
                        "previous_state": previous,
                        "reason": status.reidentified or "identity-changed",
                    },
                )
            )

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
                    profile=(
                        PermissionProfile.READ_ONLY
                        if turn.read_only
                        else profile_for_session(record)
                    ),
                    approval_prompt=self._prompt_for_current_turn,
                    question_prompt=lambda args, token, deadline: self.questions.ask(
                        turn,
                        self._activity_cb(turn),
                        # A model cannot dress a question up as an offer.
                        {key: value for key, value in args.items() if key != "_host_offer"},
                        token,
                        deadline,
                    ),
                    activity_sink=self._activity_cb(turn),
                    reasoning_effort=reasoning_effort,
                    origin_kind=_origin_kind(turn.origin),
                    session_grants=self.grants.for_session(record.id),
                    **({"remote_target": turn.remote_target} if turn.remote_target else {}),
                    **(
                        {
                            "children_host": lambda tool, args, ctx: self.children.call(
                                turn, tool, args, ctx
                            )
                        }
                        if not turn.remote_target and not turn.channel
                        else {}
                    ),
                    **(
                        {
                            "channel_host": lambda tool, args, ctx: self.channels.call(
                                turn, self._activity_cb(turn), tool, args, ctx
                            )
                        }
                        if turn.channel
                        else {}
                    ),
                )
                session.next_turn_command = turn.command
                # A child born after its mother read external content keeps the
                # guard on for its first turn (plan-sesiones-hijas 5.2).
                session.next_turn_external = bool((turn.origin or {}).get("external_content"))
                session.next_turn_external_source = str(
                    (turn.origin or {}).get("external_source") or ""
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
        # The limit is for a stage that does not move, not for the whole
        # preparation: on a slow machine each stage took a few seconds and their
        # sum passed the limit while every one of them was progressing (SOIN,
        # 23-oct).
        deadline = time.monotonic() + PREPARATION_TIMEOUT_S
        watched = turn.preparation_stage
        while True:
            if turn.cancel_requested.is_set():
                abandoned.set()
                raise CancelledError("Turn cancelled during runtime preparation")
            if turn.preparation_stage != watched:
                watched = turn.preparation_stage
                deadline = time.monotonic() + PREPARATION_TIMEOUT_S
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

    # -- steering ----------------------------------------------------------
    # A message sent while a turn runs goes into that turn: the loop adds it
    # to the history after the current step (a tool round or an answer) and
    # the model continues with it. Nothing is interrupted. One that arrives
    # while the turn is finishing becomes the next turn; one left unread by a
    # turn that was stopped or failed goes back to the owner (`steer.returned`)
    # instead of starting work they just stopped.

    def steer(
        self,
        session_id: str,
        message: str,
        *,
        attachment_context: str = "",
        image_refs: list | None = None,
        attachment_metadata: list | None = None,
        now: bool = False,
        prefix: str | None = None,
        source: str | None = None,
        display: str | None = None,
        origin_extra: dict[str, Any] | None = None,
        queued_origin: dict[str, Any] | None = None,
        queue_if_idle: bool = True,
    ) -> dict[str, Any]:
        """`attachment_context`, `image_refs` and `attachment_metadata` come from
        the same preparation as a normal message (plan 0.4.0 #54): the files
        reach the model with the text when it reads it.

        With no turn to steer, the message waits for the next turn with
        `queued_origin` (a mother's message stays hers, #100); with
        `queue_if_idle` false it is not queued at all."""
        record = self._services.sessions.show(session_id)
        text = (message or "").strip()
        if not text and attachment_metadata:
            text = "(attached files)"
        if not text:
            raise EngineProtocolError(errors.INVALID_PARAMS, "Message is empty.")
        if len(text) > MAX_QUEUE_MESSAGE_CHARS:
            raise EngineProtocolError(
                errors.INVALID_PARAMS,
                f"Message exceeds {MAX_QUEUE_MESSAGE_CHARS} chars.",
            )
        with self._lock:
            turn = next(
                (
                    active
                    for active in self._turns.values()
                    if active.session_id == record.id and not active.done.is_set()
                ),
                None,
            )
            if (
                turn is not None
                and turn.steering_open
                and (not turn.compaction_only or turn.continue_after)
            ):
                if len(turn.steering) >= MAX_QUEUE_DEPTH:
                    raise EngineProtocolError(
                        errors.INVALID_PARAMS,
                        f"Too many unread messages ({MAX_QUEUE_DEPTH}).",
                    )
                turn.steer_count += 1
                steer_id = f"{turn.turn_id}:steer_{turn.steer_count}"
                body = (
                    f"{attachment_context}\n\nUser request:\n{text}" if attachment_context else text
                )
                turn.steering.append(
                    ChatMessage(
                        role="user",
                        content=(
                            prefix
                            if prefix is not None
                            else (STEER_NOW_PREFIX if now else STEER_PREFIX)
                        )
                        + body,
                        display_content=display or text,
                        images=self._steer_images(turn, record.id, image_refs),
                        attachments=tuple(attachment_metadata or ()),
                        origin={
                            "kind": "user",
                            "steer_id": steer_id,
                            **({"interrupt": True} if now else {}),
                            # `parent`: the mother conversation wrote it (child sessions).
                            **({"source": source} if source else {}),
                            **(origin_extra or {}),
                        },
                    )
                )
                target = turn
                delivered = {
                    "session_id": record.id,
                    "delivery": "steer",
                    "turn_id": turn.turn_id,
                    "steer_id": steer_id,
                }
            else:
                target = None
        if target is not None:
            self._owner_wrote(target)
            if now:
                delivered["interrupted"] = self._interrupt_step(target)
            return delivered
        # No turn to steer (it just ended, or is finishing): an ordinary
        # message for the next turn, started now when the session is idle.
        if not queue_if_idle:
            return {"session_id": record.id, "delivery": "none"}
        if queued_origin is not None:
            queued = self.queue_add(
                record.id,
                (prefix or "") + text,
                origin=queued_origin,
                display_message=text if prefix else None,
            )
        else:
            queued = self.queue_add(record.id, text)
        self._start_next_queued(record.id)
        return {"session_id": record.id, "delivery": "queued", "position": queued["position"]}

    @staticmethod
    def _owner_wrote(turn: _ActiveTurn) -> None:
        """A message for the running turn: its waits end so it is read now (P7)."""
        session = turn.session
        context = getattr(session, "context", None) if session is not None else None
        tool_ctx = getattr(context, "tool_ctx", None)
        wrote = getattr(getattr(tool_ctx, "turn_state", None), "owner_wrote", None)
        if wrote is not None:
            wrote.set()

    def steer_promote(self, session_id: str, steer_id: str) -> dict[str, Any]:
        """«Enviar ahora» on a message that waits (P7): cut the step and read it now."""
        record = self._services.sessions.show(session_id)
        with self._lock:
            turn, index = self._queued_steer(record.id, steer_id)
            current = turn.steering[index]
            content = current.content or ""
            if content.startswith(STEER_PREFIX):
                content = STEER_NOW_PREFIX + content[len(STEER_PREFIX) :]
            turn.steering[index] = replace(
                current,
                content=content,
                origin={**(current.origin or {}), "interrupt": True},
            )
            turn_id = turn.turn_id
        interrupted = self._interrupt_step(turn)
        payload = {
            "session_id": record.id,
            "turn_id": turn_id,
            "steer_id": steer_id,
            "interrupt": True,
        }
        self._emit(event("steer.updated", payload))
        return {**payload, "status": "promoted", "interrupted": interrupted}

    def _interrupt_step(self, turn: _ActiveTurn) -> list[str]:
        """End the current step, not the turn, so the message is read now (#31).

        An answer being written is cut (it stays as partial); a command in the
        foreground moves to the background (#32); a pending approval counts as
        «no». Background processes and subagents are left alone.
        """
        from rinari.tools.native.shell import request_background, running_foreground

        done: list[str] = []
        session = turn.session
        context = getattr(session, "context", None) if session is not None else None
        self._owner_wrote(turn)
        if context is not None:
            if context.step_interrupt is None:
                context.step_interrupt = threading.Event()
            context.step_interrupt.set()
            done.append("answer")
        for call_id in running_foreground(turn.session_id):
            if request_background(turn.session_id, call_id, "steer_now"):
                done.append(f"command:{call_id}")
        with self._lock:
            pending = [
                approval.approval_id
                for approval in self._approvals.values()
                if approval.session_id == turn.session_id and not approval.decided.is_set()
            ]
        for approval_id in pending:
            with contextlib.suppress(Exception):
                self.resolve_approval(approval_id, "deny")
                done.append(f"approval:{approval_id}")
        return done

    def _steer_images(self, turn: _ActiveTurn, session_id: str, image_refs: list | None) -> tuple:
        """Pixels only for a model that sees; otherwise the manifest names the
        files and the model opens them with its tools (plan 0.4.0 #11)."""
        if not image_refs:
            return ()
        from rinari.models.images import references
        from rinari.runtime.vision import visual_status

        session = turn.session
        gateway = getattr(session, "gateway", None) if session is not None else None
        if gateway is not None:
            with contextlib.suppress(Exception):
                if not visual_status(gateway).available:
                    return ()
        return tuple(references(self._services.artifacts, session_id, image_refs))

    def _queued_steer(self, session_id: str, steer_id: str) -> tuple[_ActiveTurn, int]:
        """The running turn and the index of an unread message; caller holds the lock."""
        for active in self._turns.values():
            if active.session_id != session_id or active.done.is_set():
                continue
            for index, item in enumerate(active.steering):
                if (item.origin or {}).get("steer_id") == steer_id:
                    return active, index
        raise EngineProtocolError(
            "STEER_ALREADY_READ",
            "Rinari already read this message.",
            details={"steer_id": steer_id},
        )

    def steer_update(self, session_id: str, steer_id: str, message: str) -> dict[str, Any]:
        """Edit a message sent during the turn while Rinari has not read it (#54)."""
        record = self._services.sessions.show(session_id)
        text = (message or "").strip()
        if not text:
            raise EngineProtocolError(errors.INVALID_PARAMS, "Message is empty.")
        if len(text) > MAX_QUEUE_MESSAGE_CHARS:
            raise EngineProtocolError(
                errors.INVALID_PARAMS, f"Message exceeds {MAX_QUEUE_MESSAGE_CHARS} chars."
            )
        with self._lock:
            turn, index = self._queued_steer(record.id, steer_id)
            current = turn.steering[index]
            content = current.content or ""
            # Keep the attachment preamble; replace the owner's words only.
            marker = "\n\nUser request:\n"
            if marker in content:
                content = content.rsplit(marker, 1)[0] + marker + text
            else:
                content = STEER_PREFIX + text
            turn.steering[index] = replace(current, content=content, display_content=text)
            turn_id = turn.turn_id
        payload = {
            "session_id": record.id,
            "turn_id": turn_id,
            "steer_id": steer_id,
            "message": text,
        }
        self._emit(event("steer.updated", payload))
        return {**payload, "status": "updated"}

    def steer_cancel(self, session_id: str, steer_id: str) -> dict[str, Any]:
        """Withdraw a message sent during the turn before Rinari reads it (#54)."""
        record = self._services.sessions.show(session_id)
        with self._lock:
            turn, index = self._queued_steer(record.id, steer_id)
            turn.steering.pop(index)
            turn_id = turn.turn_id
        payload = {"session_id": record.id, "turn_id": turn_id, "steer_id": steer_id}
        self._emit(event("steer.cancelled", payload))
        return {**payload, "status": "cancelled"}

    def _drain_steering(self, turn: _ActiveTurn) -> list[ChatMessage]:
        with self._lock:
            taken, turn.steering = turn.steering, []
        return taken

    def _close_steering(self, turn: _ActiveTurn) -> list[ChatMessage]:
        with self._lock:
            turn.steering_open = False
            taken, turn.steering = turn.steering, []
        return taken

    def _settle_steering(self, turn: _ActiveTurn, leftover: list[ChatMessage]) -> None:
        # A child's report the mother did not read stays unreported: her turn's
        # end wakes her with it (#100).
        leftover = [m for m in leftover if (m.origin or {}).get("source") != "child_report"]
        if not leftover:
            return
        texts = [item.display_content or item.content for item in leftover]
        if turn.terminal_event == "turn.completed":
            # A mother's message keeps its origin, so the child's turn reports
            # back at once and not as if the owner had written it (#100).
            parents = [m for m in leftover if (m.origin or {}).get("source") == "parent"]
            owners = [m for m in leftover if (m.origin or {}).get("source") != "parent"]
            queued = []
            if owners:
                queued.append(
                    _QueuedPrompt(
                        message="\n\n".join(m.display_content or m.content for m in owners),
                        origin={"kind": "user"},
                    )
                )
            if parents:
                queued.append(
                    _QueuedPrompt(
                        message="\n\n".join(m.content for m in parents),
                        display_message="\n\n".join(
                            m.display_content or m.content for m in parents
                        ),
                        origin={"kind": "user", "source": "parent_message"},
                    )
                )
            with self._lock:
                pending = self._queue.setdefault(turn.session_id, collections.deque())
                for item in reversed(queued):
                    pending.appendleft(item)
            self._emit(
                event(
                    "session.queue.updated",
                    {
                        "session_id": turn.session_id,
                        "pending": self._pending_count(turn.session_id),
                    },
                )
            )
            return
        self._emit(
            event(
                "steer.returned",
                {
                    "session_id": turn.session_id,
                    "turn_id": turn.turn_id,
                    "messages": texts,
                    "reason": (turn.terminal_event or "turn.failed").removeprefix("turn."),
                },
            )
        )

    # -- prompt queue ------------------------------------------------------
    # Queued messages run FIFO after the live turn ends, preserving normal
    # turn boundaries and approvals. Bounded; engine-process memory only.

    def queue_add(
        self,
        session_id: str,
        message: str,
        *,
        origin: dict[str, Any] | None = None,
        display_message: str | None = None,
        memory_origin: str = "interactive",
    ) -> dict[str, Any]:
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
            pending.append(
                _QueuedPrompt(
                    message=text,
                    origin=origin or {"kind": "user"},
                    display_message=display_message,
                    memory_origin=memory_origin,
                )
            )
            position = len(pending)
        self._emit(
            event(
                "session.queue.updated",
                {"session_id": record.id, "pending": self._pending_count(record.id)},
            )
        )
        return {"session_id": record.id, "position": position, "pending": position}

    def _pending_count(self, session_id: str) -> int:
        return len(self._queue.get(session_id, ()))

    def queue_list(self, session_id: str) -> dict[str, Any]:
        """Legacy `queue: string[]` plus typed `entries`."""
        record = self._services.sessions.show(session_id)
        with self._lock:
            manual = list(self._queue.get(record.id, ()))
        entries: list[dict[str, Any]] = [
            {
                "message_id": None,
                "message": item.message,
                "state": "queued",
                "origin": item.origin or {"kind": "user"},
                "display_message": item.display_message,
            }
            for item in manual
        ]
        queued_texts = [item.message for item in manual]
        return {
            "session_id": record.id,
            "queue": queued_texts,
            "pending": len(queued_texts),
            "entries": entries,
        }

    def queue_clear(self, session_id: str) -> dict[str, Any]:
        record = self._services.sessions.show(session_id)
        with self._lock:
            dropped = list(self._queue.pop(record.id, ()))
        self._emit(event("session.queue.updated", {"session_id": record.id, "pending": 0}))
        listener = self.queue_cleared
        if listener is not None and dropped:
            try:
                listener(record.id, [item.origin or {"kind": "user"} for item in dropped])
            except Exception:
                logger.exception("queue cleared listener failed")
        return {"session_id": record.id, "removed": len(dropped)}

    def queue_discard(self, session_id: str, run_ids: set[str]) -> int:
        """Take scheduled runs out of a session's queue (their task was deleted or
        turned off); the rest of the queue stays as it was."""
        with self._lock:
            pending = self._queue.get(session_id)
            if not pending:
                return 0
            kept = collections.deque(
                item
                for item in pending
                if not (
                    (item.origin or {}).get("kind") == "schedule"
                    and (item.origin or {}).get("run_id") in run_ids
                )
            )
            removed = len(pending) - len(kept)
            if kept:
                self._queue[session_id] = kept
            else:
                self._queue.pop(session_id, None)
        if removed:
            self._emit(
                event(
                    "session.queue.updated",
                    {"session_id": session_id, "pending": self._pending_count(session_id)},
                )
            )
        return removed

    def queue_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        """Explicit intervention: the session's queue runs again."""
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise EngineProtocolError(errors.INVALID_PARAMS, "Param 'session_id' must be a string.")
        record = self._services.sessions.show(session_id)
        resumed = 0
        self._emit(
            event(
                "session.queue.updated",
                {"session_id": record.id, "pending": self._pending_count(record.id)},
            )
        )
        self.start_next_queued(record.id)
        return {"session_id": record.id, "resumed": resumed}

    def start_next_queued(self, session_id: str) -> None:
        """Single scheduler per session: the queued messages, in order."""
        self._start_next_queued(session_id)

    # -- goals (plan 0.4.0 #23) ---------------------------------------------------

    def _goal_service(self) -> Any:
        return getattr(self._services, "goals", None)

    def emit_goal(self, goal: Any, session_id: str) -> None:
        self._emit(
            event(
                "goal.updated",
                {"session_id": session_id, "goal": goal.as_dict() if goal is not None else None},
            )
        )

    def _goal_after_turn(self, turn: _ActiveTurn) -> None:
        """Fold a finished turn into the goal and start the next iteration.

        Runs after the owner's queue had its chance: a queued message goes
        first and the goal goes on when that turn ends.
        """
        service = self._goal_service()
        if service is None:
            return
        from rinari.goals.service import TurnOutcome

        try:
            origin = turn.origin or {}
            terminal = (turn.terminal_event or "turn.completed").removeprefix("turn.")
            if origin.get("source") == "goal":
                result = turn.goal_result
                usage = getattr(result, "usage", None)
                tokens = int(getattr(usage, "input_tokens", 0) or 0) + int(
                    getattr(usage, "output_tokens", 0) or 0
                )
                checklist = None
                checklist_service = self._checklist_service()
                if checklist_service is not None:
                    with contextlib.suppress(Exception):
                        current = checklist_service.get(turn.session_id)
                        checklist = current.revision if current is not None else None
                outcome = TurnOutcome(
                    terminal=terminal,
                    error_code=turn.goal_error[0],
                    error_message=turn.goal_error[1],
                    recoverable=bool(getattr(result, "recoverable", False)),
                    gate=getattr(result, "completion", None),
                    tokens=tokens,
                    seconds=max(0.0, time.time() - turn.started_at),
                    files_changed=turn.files_changed,
                    checklist_revision=checklist,
                )
                goal, go_on = service.after_turn(turn.session_id, outcome)
                self.emit_goal(goal, turn.session_id)
            else:
                goal = service.pursuing(turn.session_id)
                if goal is not None and terminal == "cancelled":
                    # The owner pressed Stop: nothing starts behind their back.
                    self.emit_goal(service.pause(turn.session_id, "stopped"), turn.session_id)
                    return
                go_on = goal is not None
            if go_on:
                self.start_goal_turn(turn.session_id)
        except Exception:
            logger.exception("goal runner failed")

    def start_goal_turn(self, session_id: str) -> dict[str, Any] | None:
        """Start the next iteration of a pursued goal, if the session is free."""
        service = self._goal_service()
        if service is None:
            return None
        goal = service.pursuing(session_id)
        if goal is None:
            return None
        with self._lock:
            busy = any(
                not active.done.is_set() and active.session_id == session_id
                for active in self._turns.values()
            ) or bool(self._queue.get(session_id))
        if busy:
            return None
        message = service.continuation(goal)
        goal = service.begin_iteration(session_id)
        if goal is None:
            return None
        self.emit_goal(goal, session_id)
        try:
            return self.start_turn(
                session_id,
                message,
                origin={
                    "kind": "user",
                    "source": "goal",
                    "goal_id": goal.goal_id,
                    "iteration": goal.iteration,
                },
                display_message=goal.objective,
                memory_origin="automation",
            )
        except EngineProtocolError as exc:
            if exc.code != errors.TURN_RUNNING:
                paused = service.pause(session_id, exc.message)
                self.emit_goal(paused, session_id)
            return None

    def kick_queue(self, session_id: str) -> None:
        """Run the session's queue now if nothing holds it."""
        self._start_next_queued(session_id)

    def _start_next_queued(self, session_id: str) -> None:
        hold = self.queue_hold
        if hold is not None:
            try:
                if hold(session_id):
                    return
            except Exception:
                logger.exception("queue hold check failed")
        with self._lock:
            if any(
                not turn.done.is_set() and turn.session_id == session_id
                for turn in self._turns.values()
            ):
                return
            pending = self._queue.get(session_id)
            item = pending.popleft() if pending else None
            if pending is not None and not pending:
                self._queue.pop(session_id, None)
        if item is not None:
            self._emit(
                event(
                    "session.queue.updated",
                    {"session_id": session_id, "pending": self._pending_count(session_id)},
                )
            )
            try:
                self.start_turn(
                    session_id,
                    item.message,
                    display_message=item.display_message,
                    origin=item.origin,
                    memory_origin=item.memory_origin,
                )
            except EngineProtocolError:
                with self._lock:
                    self._queue.setdefault(session_id, collections.deque()).appendleft(item)
            return

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
            if event_name == "steer.applied" and payload.get("source") == "child_report":
                # The mother read the reports inside her turn (#100).
                self.children.reports_read(turn.session_id, payload.get("children") or [])
            effective = (
                payload.get("child_event", event_name)
                if event_name == "agent.activity"
                else event_name
            )
            if effective.startswith("usage.call."):
                update = turn.token_usage.observe(effective, payload)
                if update is not None:
                    _on_activity(
                        "usage.updated",
                        {
                            **update,
                            "_checkpoint": effective == "usage.call.completed"
                            and update["source"] != "estimated",
                        },
                    )
                elif turn.token_usage.last and "usage:turn" in turn.activities:
                    # Throttled: nothing is emitted, but a UI reload reads the
                    # latest estimate. Same lock and replacement as every other
                    # write to `turn.activities`, which runtime_state() copies.
                    with self._lock:
                        turn.activities["usage:turn"] = {
                            **turn.activities["usage:turn"],
                            **turn.token_usage.last,
                        }
                return
            persist_usage = payload.get("_checkpoint", False)
            payload = {key: value for key, value in payload.items() if key != "_checkpoint"}
            if event_name in {"turn.completed", "turn.failed", "turn.cancelled", "turn.stopped"}:
                # The list ends in the state the outcome deserves, and the
                # client hears it before the terminal event.
                self._checklist_settle(turn, event_name, _on_activity)
                update = turn.token_usage.finish()
                if update is not None:
                    _on_activity("usage.updated", {**update, "_checkpoint": True})
                with self._lock:
                    pending_visual = [
                        dict(item)
                        for item in turn.activities.values()
                        if item.get("event")
                        in {"vision.started", "vision.queued", "vision.preparing"}
                    ]
                for item in pending_visual:
                    _on_activity(
                        "vision.cancelled" if event_name == "turn.cancelled" else "vision.failed",
                        {**item, "error": "Turn ended before visual analysis completed"},
                    )
            effective_event = (
                payload.get("child_event", event_name)
                if event_name == "agent.activity"
                else event_name
            )
            if not effective_event.startswith(_OWNER_TEXT_EVENTS):
                # The desktop shows, stores and exports these payloads
                # (timeline, history, diagnostics): a credential in a command
                # or an output is hidden once, here. The owner's own words
                # (steering, answers) are shown as written.
                payload = redact_value(payload)
            safe = {**payload, "turn_id": turn.turn_id, "session_id": turn.session_id}
            if event_name != "usage.updated":
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
            presentation = safe.get("presentation")
            if isinstance(presentation, dict):
                safe["presentation"] = _safe_presentation(presentation)
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
                if effective_event == "model.content.delta":
                    delta = str(safe.get("delta") or "")
                    safe["delta"] = delta[:MAX_DELTA_CHARS]
                    current = {**current, "content": str(current.get("content") or "") + delta}
                elif effective_event == "model.content.completed":
                    current = {**current, "content": str(safe.get("content") or "")}
                elif effective_event == "model.retrying":
                    # The retry streams its answer again from the start.
                    current = {**current, "content": ""}
                elif effective_event == "tool.output.delta":
                    stream = safe.get("stream")
                    delta = str(safe.get("delta") or "")
                    if stream in {"stdout", "stderr"} and delta:
                        presentation = dict(current.get("presentation") or {})
                        presentation.setdefault("kind", "command")
                        presentation.setdefault("tool", safe.get("tool"))
                        presentation["status"] = "running"
                        existing = str(presentation.get(stream) or "")
                        sequences = dict(presentation.get("stream_sequences") or {})
                        sequence = safe.get("stream_seq")
                        previous = sequences.get(stream, 0)
                        if not isinstance(sequence, int) or sequence > previous:
                            combined = existing + delta
                            if len(combined) > MAX_STREAM_CHARS:
                                combined = combined[:MAX_STREAM_CHARS]
                                presentation["truncated"] = True
                            presentation[stream] = combined
                            if isinstance(sequence, int):
                                sequences[stream] = sequence
                        presentation["stream_sequences"] = sequences
                        current = {**current, "presentation": presentation}
                turn.activities[activity_key] = {**current, **safe, "event": event_name}
            if event_name in {"turn.completed", "turn.failed", "turn.cancelled", "turn.stopped"}:
                self.operations.finish(turn.turn_id, event_name.removeprefix("turn."))
            if effective_event != "model.content.delta" and (
                event_name != "usage.updated" or persist_usage
            ):
                self._persist_activity(event_name, safe)
            self._emit(event(event_name, safe))
            if (
                event_name == "model.content.completed"
                and not turn.model_notice_checked
                and str(safe.get("content") or "").strip()
            ):
                turn.model_notice_checked = True
                try:
                    notice = self._model_change_notice(turn, safe)
                except Exception:
                    # A notice is presentation; it never breaks the turn.
                    logger.exception("model change notice failed")
                    notice = None
                if notice is not None:
                    _on_activity("model.changed", notice)

        def _serialized(event_name: str, payload: dict[str, Any]) -> None:
            # Preparation and cancellation can race on separate workers. Keep
            # the persisted/emitted terminal boundary atomic with other activity.
            with turn.activity_lock:
                if turn.terminal_emitted:
                    return
                _on_activity(event_name, payload)
                if event_name in {
                    "turn.completed",
                    "turn.failed",
                    "turn.cancelled",
                    "turn.stopped",
                }:
                    turn.terminal_emitted = True
                    turn.terminal_event = event_name

        return _serialized

    def _model_change_notice(
        self, turn: _ActiveTurn, completed: dict[str, Any]
    ) -> dict[str, Any] | None:
        """`model.changed` when this turn's first text comes from another model.

        It compares the model that writes now with the last one that wrote
        text in the session: a model that was selected but failed before
        writing never counts, so A, then B failing, then C writing reads
        "A → C". Both sides are stored as they are named today, so the notice
        keeps its names after a model is renamed or removed.
        """
        call_id = completed.get("model_call_id")
        with self._lock:
            current = (turn.activities.get(f"model:{call_id}") or {}).get("model")
        if not isinstance(current, str) or not current:
            return None
        db = self._services.ctx.db
        last = db.query_one(
            "SELECT turn_id, payload_json FROM session_events WHERE session_id = ? "
            "AND type = 'model.content.completed' AND turn_id IS NOT NULL AND turn_id != ? "
            "ORDER BY seq DESC LIMIT 1",
            (turn.session_id, turn.turn_id),
        )
        if last is None:
            return None
        previous_call = _json_payload(last["payload_json"]).get("model_call_id")
        previous = None
        for row in db.query(
            "SELECT payload_json FROM session_events WHERE session_id = ? AND turn_id = ? "
            "AND type = 'model.started' ORDER BY seq",
            (turn.session_id, last["turn_id"]),
        ):
            started = _json_payload(row["payload_json"])
            if started.get("model_call_id") == previous_call:
                previous = started.get("model")
        if not isinstance(previous, str) or not previous or previous == current:
            return None
        return {
            "after_model_call_id": call_id,
            "previous": self._model_snapshot(turn.session_id, previous),
            "next": self._model_snapshot(turn.session_id, current),
        }

    def _model_snapshot(self, session_id: str, model_id: str) -> dict[str, Any]:
        ctx = self._services.ctx
        model = ctx.model_repo.get(model_id)
        if model is None:
            # Gone from the catalog: the last notice that named it, if any.
            row = ctx.db.query_one(
                "SELECT payload_json FROM session_events WHERE session_id = ? "
                "AND type = 'model.changed' ORDER BY seq DESC LIMIT 1",
                (session_id,),
            )
            named = _json_payload(row["payload_json"]).get("next") if row else None
            if isinstance(named, dict) and named.get("model_id") == model_id:
                return named
            return {"model_id": model_id}
        provider = ctx.provider_repo.get(model.provider_id)
        return {
            "model_id": model.id,
            "alias": model.alias,
            "provider_model_id": model.provider_model_id,
            "provider_id": model.provider_id,
            "provider_alias": provider.alias if provider is not None else None,
        }

    def _activity_key(self, turn: _ActiveTurn, event_name: str, payload: dict[str, Any]) -> str:
        if event_name == "usage.updated":
            return "usage:turn"
        if event_name == "agent.activity":
            child = dict(payload)
            child.pop("agent_id", None)
            return f"agent:{payload.get('agent_id')}:" + self._activity_key(
                turn, str(payload.get("child_event", "")), child
            )
        if payload.get("vision_id"):
            return f"vision:{payload['vision_id']}:{payload.get('attempt_id', 1)}"
        if event_name == "steer.applied":
            return f"steer:{payload.get('steer_id')}"
        if event_name.startswith("followup.") and (
            payload.get("suggestion_id") or (payload.get("suggestion") or {}).get("id")
        ):
            # Suggested and resolved update one note in the timeline.
            key = payload.get("suggestion_id") or payload["suggestion"]["id"]
            return f"followup:{key}"
        if event_name == "file.presented":
            # Its own card, apart from the file.present tool row (#35).
            return f"present:{payload.get('present_id')}"
        if event_name == "checklist.updated":
            # One card per turn that updates in place.
            return f"checklist:{turn.turn_id}"
        if payload.get("tool_call_id"):
            return f"tool:{payload['tool_call_id']}"
        if payload.get("model_call_id"):
            return f"model:{payload['model_call_id']}"
        if payload.get("request_id") and event_name.startswith("question."):
            return f"question:{payload['request_id']}"
        if payload.get("approval_id"):
            return f"approval:{payload['approval_id']}"
        if event_name.startswith("memory.candidate.") and payload.get("candidate_id"):
            # created and resolved update one card in the timeline.
            return f"memory-candidate:{payload['candidate_id']}"
        if event_name == "memory.remembered" and payload.get("memory_id"):
            return f"memory:{payload['memory_id']}"
        if event_name.startswith("turn.changes."):
            change_id = payload.get("id") or payload.get("changeset_id") or turn.turn_id
            return f"changeset:{change_id}"
        if event_name == "governor.compact":
            return (
                f"context:compact:{payload.get('compaction_id') or payload.get('history_size', 0)}"
            )
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
            self._lock,
            (
                contextlib.nullcontext()
                if event_name.startswith("question.")
                else contextlib.suppress(Exception)
            ),
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
            if event_name in _TOOL_ENDS and payload.get("tool_call_id"):
                # The end carries the output: the live chunks go (#91).
                self._services.ctx.event_repo.compact_tool_output(
                    str(payload["session_id"]),
                    str(payload["turn_id"]),
                    str(payload["tool_call_id"]),
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
        token = request.cancellation or token
        if token is not None and getattr(token, "cancelled", False):
            raise CancelledError("Approval abandoned: turn was cancelled.")
        turn_id = getattr(self._local, "turn_id", None)
        if turn_id is None and request.session_id:
            with self._lock:
                owner = next(
                    (
                        t
                        for t in self._turns.values()
                        if t.session_id == request.session_id and not t.done.is_set()
                    ),
                    None,
                )
            if owner is not None:
                turn_id = owner.turn_id
                token = token or getattr(owner.session, "token", None)
        approval_id = self._services.ctx.ids.new("apr")
        # What the card shows (the command, its target) is display only: the
        # decision binds to the request itself, so a credential typed into the
        # command is hidden here as in the timeline.
        target = redact_value(request.target)
        description = redact_value(request.description)
        pending = _PendingApproval(
            approval_id=approval_id,
            session_id=request.session_id,
            turn_id=turn_id,
            capability=request.capability,
            target=target,
            risk=request.risk,
            description=description,
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
            "target": target,
            "risk": request.risk,
            "description": description,
            "choices": list(request.choices),
            "rule_id": request.rule_id,
            "reusable": request.reusable,
            # Where "allow_project" keeps the grant: this project, every loose
            # chat, or (an MCP tool, mcp_tool_permissions_v1) Allow saved for
            # the tool with its server.
            "grant_scope": (
                "mcp_tool"
                if getattr(request, "on_always", None) is not None
                else "chats"
                if request.project_id in (None, "chats")
                else "project"
            ),
            # `exact`: a session grant binds to this target only.
            "binding_mode": request.binding_mode,
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
            if pending.decided.is_set():
                return {
                    "status": "resolved",
                    "approval_id": approval_id,
                    "decision": pending.decision,
                }
            pending.decision = decision
            pending.decided.set()
        return {"status": "resolved", "approval_id": approval_id, "decision": decision}


def _json_payload(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
