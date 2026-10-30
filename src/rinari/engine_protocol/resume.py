"""The resume runner: continue a conversation when its provider limit resets.

Plan 0.4.0 #24. It follows turns (as the scheduler does): a turn that failed
on a provider limit is armed when the owner's setting is «always» (or the
owner arms it from the error card), and a goal waiting for limits goes on by
itself with «always». Every TICK_S it continues what is due: the turn says
«continue where you left off» (the history kept the work), and a goal
continues as its next iteration. While a resume waits, the session's queue
waits too, so it does not fire against the same empty quota.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any

from rinari.engine_protocol import errors
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.messages import event
from rinari.resumes.service import limit_error

if TYPE_CHECKING:
    from rinari.engine_protocol.server import EngineServer

logger = logging.getLogger(__name__)

TICK_S = 20.0
CONTINUE_MESSAGE = (
    "Continue where you left off: the provider limit that stopped the last turn has reset."
)


class ResumeRunner:
    def __init__(self, server: EngineServer) -> None:
        self._server = server
        self._services = server._services
        self._turns = server._turns
        self._clock = self._services.ctx.clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._recovered = False
        # session_id -> the details of its last limit error (for «arm» from the card).
        self._last_limit: dict[str, dict[str, Any]] = {}
        # session_id -> turn_id of a continuation running now.
        self._inflight: dict[str, str] = {}
        self._turns.add_observer(self._observe)
        self._turns.queue_hold = self._holds_queue

    @property
    def service(self):
        return self._services.resumes

    # -- clock -----------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="rinari-resume", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("resume tick failed")
            self._stop.wait(TICK_S)

    def tick(self, now: float | None = None) -> list[str]:
        """Continue what is due. Returns the session ids it continued (tests)."""
        now = self._clock.now() if now is None else now
        continued: list[str] = []
        with self._lock:
            if not self._recovered:
                self._recovered = True
                for expired in self.service.expire_stale(now):
                    self._emit(expired)
            for resume in self.service.due(now):
                if resume.session_id in self._inflight or self._busy(resume.session_id):
                    continue
                if resume.mode == "poll" and not self._balance_back(resume):
                    self._emit(self.service.postpone(resume.session_id, now))
                    continue
                if self._continue(resume.session_id):
                    continued.append(resume.session_id)
        return continued

    # -- owner actions ------------------------------------------------------------

    def arm(self, session_id: str, *, resets_at: str | None = None) -> Any:
        details = dict(self._last_limit.get(session_id) or {})
        if resets_at:
            details["resets_at"] = resets_at
        resume = self._arm(session_id, details)
        self._emit(resume)
        return resume

    def now(self, session_id: str) -> bool:
        if self.service.waiting(session_id) is None:
            return False
        with self._lock:
            return self._continue(session_id)

    def cancel(self, session_id: str) -> Any:
        resume = self.service.cancel(session_id)
        if resume is not None:
            self._emit(resume)
            # The queue waited for this: let it run now.
            self._turns.kick_queue(session_id)
        return resume

    # -- following turns -------------------------------------------------------------

    def _observe(self, frame: dict[str, Any]) -> None:
        name = frame.get("event")
        payload = frame.get("payload") or {}
        session_id = payload.get("session_id")
        if not session_id or name not in (
            "turn.failed",
            "turn.completed",
            "turn.stopped",
            "turn.cancelled",
        ):
            return
        inflight = self._inflight.pop(session_id, None)
        details = limit_error(payload.get("error")) if name == "turn.failed" else None
        if details is None:
            if inflight is not None:
                finished = self.service.finish(session_id)
                if finished is not None:
                    self._emit(finished)
            return
        self._last_limit[session_id] = details
        if inflight is not None:
            # The continuation hit a limit again: another try with the new time.
            mode, resume_at = self.service.plan(
                details, self._clock.now(), self._usage_reset(session_id)
            )
            self._emit(
                self.service.rearm(
                    session_id, mode=mode, resume_at=resume_at, reason=_reason(payload)
                )
            )
            return
        if self.service.mode() == "always":
            self._emit(self._arm(session_id, details, reason=_reason(payload)))

    def _arm(self, session_id: str, details: dict[str, Any], reason: str = "") -> Any:
        now = self._clock.now()
        mode, resume_at = self.service.plan(details, now, self._usage_reset(session_id))
        goal = None
        goals = getattr(self._services, "goals", None)
        if goals is not None:
            goal = goals.get(session_id)
        return self.service.arm(
            session_id,
            provider_id=self._provider_id(session_id),
            mode=mode,
            resume_at=resume_at,
            goal_id=goal.goal_id if goal is not None and goal.state == "waiting_limits" else None,
            reason=reason,
        )

    def _continue(self, session_id: str) -> bool:
        goals = getattr(self._services, "goals", None)
        goal = goals.get(session_id) if goals is not None else None
        try:
            if goal is not None and goal.state == "waiting_limits":
                resumed = goals.resume(session_id)
                self._turns.emit_goal(resumed, session_id)
                started = self._turns.start_goal_turn(session_id)
            else:
                started = self._turns.start_turn(
                    session_id,
                    CONTINUE_MESSAGE,
                    origin={"kind": "user", "source": "auto_resume"},
                    display_message=CONTINUE_MESSAGE,
                    memory_origin="automation",
                )
        except EngineProtocolError as exc:
            if exc.code == errors.TURN_RUNNING:
                return False
            self._emit(self.service.finish(session_id, "failed", exc.message))
            return False
        if not started:
            return False
        self._inflight[session_id] = started.get("turn_id", "")
        # Due again only if this continuation fails on the limit (re-armed then).
        resume = self.service._set(session_id, resume_at=self._clock.now() + 86_400)
        self._emit(resume, continued=True)
        return True

    # -- helpers ---------------------------------------------------------------------

    def _holds_queue(self, session_id: str) -> bool:
        return session_id not in self._inflight and self.service.waiting(session_id) is not None

    def _busy(self, session_id: str) -> bool:
        return bool(self._turns.has_active_turn(session_id))

    def _provider_id(self, session_id: str) -> str | None:
        try:
            record = self._services.sessions.show(session_id)
            model = self._services.models.resolve(record.model_id) if record.model_id else None
            if model is not None:
                return model.provider_id
            current = self._services.providers.current()
            return current.provider.id if current is not None else None
        except Exception:
            return None

    def _usage(self):
        server = self._server
        if not hasattr(server, "_provider_usage_service"):
            from rinari.providers.usage import ProviderUsageService

            server._provider_usage_service = ProviderUsageService(self._services.providers)
        return server._provider_usage_service

    def _usage_reset(self, session_id: str) -> str | None:
        """The nearest reset of an exhausted usage window, when the error gave none."""
        provider_id = self._provider_id(session_id)
        if provider_id is None:
            return None
        try:
            snapshot = self._usage().get(provider_id)
        except Exception:
            return None
        now = self._clock.now()
        times = []
        for window in snapshot.get("windows") or []:
            remaining = window.get("remaining_percent")
            resets = window.get("resets_at")
            if remaining is not None and remaining <= 0 and isinstance(resets, str):
                try:
                    moment = datetime.fromisoformat(resets).timestamp()
                except ValueError:
                    continue
                if moment > now:
                    times.append((moment, resets))
        return min(times)[1] if times else None

    def _balance_back(self, resume) -> bool:
        if resume.provider_id is None:
            return True
        try:
            snapshot = self._usage().get(resume.provider_id, refresh=True)
        except Exception:
            return True  # nothing to read: try the continuation
        balances = snapshot.get("balances") or []
        if not balances:
            return True
        for balance in balances:
            if balance.get("unlimited"):
                return True
            try:
                if float(balance.get("remaining") or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _emit(self, resume, *, continued: bool = False) -> None:
        if resume is None:
            return
        payload = {"session_id": resume.session_id, "resume": resume.as_dict()}
        if continued:
            payload["continued"] = True
        self._turns.emit_external(event("session.resume.updated", payload))


def _reason(payload: dict[str, Any]) -> str:
    error = payload.get("error") or {}
    return str(error.get("message") or "")[:300] if isinstance(error, dict) else ""


__all__ = ["CONTINUE_MESSAGE", "ResumeRunner"]
