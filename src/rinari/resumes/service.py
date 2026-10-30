"""A conversation waiting for a provider limit to reset, to continue by itself.

A turn cut by a limit (rate, quota, credits) can be armed to continue when the
limit resets: at the time the provider gave (`mode=at`), or, for credit-only
providers that give no time, when the balance is back (`mode=poll`, checked
every 10 minutes, backing off to 30). When it is due the runner continues the
conversation («continue where you left off»), never resends the request. A
second limit re-arms it with the new time, up to MAX_ATTEMPTS.

It needs the app open (also in the tray): the Engine lives with it. On start,
a resume whose time passed less than 12 hours ago continues; an older one only
says so.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rinari.shared.clock import now_iso

RESUME_MODES = ("ask", "always", "never")
SETTING_KEY = "auto_resume_on_limits"
MAX_ATTEMPTS = 5
POLL_FIRST_S = 10 * 60
POLL_MAX_S = 30 * 60
# Without any time from the provider, the first look.
DEFAULT_WAIT_S = 15 * 60
STALE_AFTER_S = 12 * 3600
LIMIT_CODES = frozenset({"RATE_LIMIT", "QUOTA_EXHAUSTED"})


@dataclass(frozen=True, slots=True)
class Resume:
    session_id: str
    provider_id: str | None
    mode: str
    resume_at: float
    state: str
    attempts: int
    goal_id: str | None
    reason: str
    revision: int
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "provider_id": self.provider_id,
            "mode": self.mode,
            "resume_at": datetime.fromtimestamp(self.resume_at, UTC).isoformat(),
            "state": self.state,
            "attempts": self.attempts,
            "goal_id": self.goal_id,
            "reason": self.reason,
            "revision": self.revision,
            "updated_at": self.updated_at,
        }


def limit_error(error: dict[str, Any] | None) -> dict[str, Any] | None:
    """The details of a turn error that is a provider limit, or None."""
    if not isinstance(error, dict):
        return None
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    code = str(details.get("provider_error_code") or "")
    if code in LIMIT_CODES or details.get("limit_kind") in ("quota", "rate"):
        return details
    return None


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class ResumeService:
    def __init__(self, ctx) -> None:
        self._ctx = ctx
        self._lock = threading.Lock()

    # -- the owner's setting -----------------------------------------------------

    def mode(self) -> str:
        value = self._ctx.config_repo.get(SETTING_KEY)
        raw = getattr(value, "value", value)
        return raw if raw in RESUME_MODES else "ask"

    def set_mode(self, mode: str) -> str:
        if mode not in RESUME_MODES:
            raise ValueError(f"mode must be one of {', '.join(RESUME_MODES)}")
        from rinari.storage.records import ConfigValue

        self._ctx.config_repo.set(
            ConfigValue(key=SETTING_KEY, value=mode, updated_at=now_iso(self._ctx.clock))
        )
        return mode

    # -- reads -----------------------------------------------------------------------

    def get(self, session_id: str) -> Resume | None:
        row = self._ctx.db.query_one(
            "SELECT * FROM session_resumes WHERE session_id = ?", (session_id,)
        )
        return _from_row(row) if row is not None else None

    def waiting(self, session_id: str) -> Resume | None:
        current = self.get(session_id)
        return current if current is not None and current.state == "waiting" else None

    def due(self, now: float) -> list[Resume]:
        rows = self._ctx.db.query(
            "SELECT * FROM session_resumes WHERE state = 'waiting' AND resume_at <= ?", (now,)
        )
        return [_from_row(row) for row in rows]

    # -- arming ------------------------------------------------------------------------

    def plan(self, details: dict[str, Any], now: float, usage_reset: str | None = None):
        """(mode, resume_at) for a limit error: its own time, the usage window's, or poll."""
        moment = _epoch(details.get("resets_at")) or _epoch(usage_reset)
        if moment is not None:
            # A few seconds after the reset: providers count their window on their clock.
            return "at", max(now + 5, moment + 10)
        if details.get("limit_kind") == "quota" or details.get("provider_error_code") == (
            "QUOTA_EXHAUSTED"
        ):
            return "poll", now + POLL_FIRST_S
        return "at", now + DEFAULT_WAIT_S

    def arm(
        self,
        session_id: str,
        *,
        provider_id: str | None,
        mode: str,
        resume_at: float,
        goal_id: str | None = None,
        reason: str = "",
    ) -> Resume:
        now = now_iso(self._ctx.clock)
        with self._lock:
            current = self.get(session_id)
            attempts = current.attempts if current is not None and current.state == "waiting" else 0
            resume = Resume(
                session_id=session_id,
                provider_id=provider_id,
                mode=mode,
                resume_at=resume_at,
                state="waiting",
                attempts=attempts,
                goal_id=goal_id,
                reason=reason[:300],
                revision=(current.revision if current is not None else 0) + 1,
                created_at=current.created_at if current is not None else now,
                updated_at=now,
            )
            self._write(resume)
        return resume

    def rearm(self, session_id: str, *, mode: str, resume_at: float, reason: str) -> Resume:
        """The continuation hit a limit again: another try, or give up after MAX."""
        current = self.get(session_id)
        attempts = (current.attempts if current is not None else 0) + 1
        if attempts >= MAX_ATTEMPTS:
            return self._set(session_id, state="failed", attempts=attempts, reason=reason)
        return self._set(
            session_id,
            state="waiting",
            mode=mode,
            resume_at=resume_at,
            attempts=attempts,
            reason=reason,
        )

    def postpone(self, session_id: str, now: float) -> Resume:
        """Poll mode found no balance yet: look again later, backing off."""
        current = self.get(session_id)
        attempts = (current.attempts if current is not None else 0) + 1
        wait = min(POLL_MAX_S, POLL_FIRST_S * (2 ** min(attempts, 2)))
        return self._set(session_id, resume_at=now + wait, attempts=attempts)

    def finish(self, session_id: str, state: str = "done", reason: str = "") -> Resume | None:
        if self.get(session_id) is None:
            return None
        return self._set(session_id, state=state, reason=reason)

    def cancel(self, session_id: str) -> Resume | None:
        current = self.waiting(session_id)
        return self._set(session_id, state="cancelled") if current is not None else current

    def expire_stale(self, now: float) -> list[Resume]:
        """At engine start: what waited past its time by more than 12 h only says so."""
        rows = self._ctx.db.query(
            "SELECT * FROM session_resumes WHERE state = 'waiting' AND resume_at < ?",
            (now - STALE_AFTER_S,),
        )
        return [
            self._set(str(row["session_id"]), state="expired", reason="the app was closed")
            for row in rows
        ]

    # -- storage -------------------------------------------------------------------------

    def _set(self, session_id: str, **changes: Any) -> Resume:
        with self._lock:
            current = self.get(session_id)
            assert current is not None
            values = {field: getattr(current, field) for field in Resume.__dataclass_fields__}  # type: ignore[attr-defined]
            values.update(changes)
            values["revision"] = current.revision + 1
            values["updated_at"] = now_iso(self._ctx.clock)
            resume = Resume(**values)
            self._write(resume)
        return resume

    def _write(self, resume: Resume) -> None:
        self._ctx.db.execute(
            "INSERT INTO session_resumes (session_id, provider_id, mode, resume_at, state, "
            "attempts, goal_id, reason, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
            "provider_id = excluded.provider_id, mode = excluded.mode, "
            "resume_at = excluded.resume_at, state = excluded.state, "
            "attempts = excluded.attempts, goal_id = excluded.goal_id, "
            "reason = excluded.reason, revision = excluded.revision, "
            "created_at = excluded.created_at, updated_at = excluded.updated_at",
            (
                resume.session_id,
                resume.provider_id,
                resume.mode,
                resume.resume_at,
                resume.state,
                resume.attempts,
                resume.goal_id,
                resume.reason,
                resume.revision,
                resume.created_at,
                resume.updated_at,
            ),
        )


def _from_row(row) -> Resume:
    return Resume(
        session_id=str(row["session_id"]),
        provider_id=row["provider_id"],
        mode=str(row["mode"]),
        resume_at=float(row["resume_at"]),
        state=str(row["state"]),
        attempts=int(row["attempts"]),
        goal_id=row["goal_id"],
        reason=str(row["reason"] or ""),
        revision=int(row["revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


__all__ = ["RESUME_MODES", "Resume", "ResumeService", "limit_error"]
