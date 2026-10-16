"""The goal of a conversation: an objective Rinari pursues until it is achieved.

The owner sets it (`/goal <objective>`); the Engine then starts turn after
turn, each with a short harness note of where the goal stands, until one of
these ends it:

- Rinari reports it achieved (`goal.update` with evidence) and, in a project
  turn the verification gate evaluated, the gate says DONE. Any other gate
  outcome keeps the goal going, with the gate's reasons in the next note;
- Rinari reports it blocked (it needs the owner);
- three iterations in a row without progress (no files changed, no new
  checklist revision, the same summary): stalled, never an endless loop;
- a cap the owner set runs out (turns, minutes, tokens);
- the owner pauses or ends it, or presses Stop (paused).

A provider limit leaves it waiting_limits, for the resume of #24. Everything
the next turn needs to know is in the row, so a restart resumes nothing by
itself: the owner sees the state and decides.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any

from rinari.shared.clock import now_iso

GOAL_STATES = (
    "pursuing",
    "paused",
    "waiting_limits",
    "achieved",
    "blocked",
    "stalled",
    "budget_limited",
    "cleared",
)
# States in which the goal is over until the owner acts.
FINISHED = frozenset({"achieved", "cleared"})
STALL_AFTER = 3
OBJECTIVE_MAX_CHARS = 2000
_BUDGET_KEYS = ("turns", "minutes", "tokens")
# Provider taxonomy codes (providers.errors.ProviderErrorCode) for a limit.
_LIMIT_CODES = frozenset({"RATE_LIMIT", "QUOTA_EXHAUSTED"})


class GoalError(ValueError):
    """A goal request that cannot be carried out as it is."""


@dataclass(frozen=True, slots=True)
class Goal:
    session_id: str
    goal_id: str
    objective: str
    state: str
    budget: dict[str, int]
    iteration: int
    tokens_used: int
    active_seconds: float
    quiet_iterations: int
    assessment: dict[str, Any]
    reason: str
    revision: int
    started_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "goal_id": self.goal_id,
            "objective": self.objective,
            "state": self.state,
            "budget": dict(self.budget),
            "iteration": self.iteration,
            "tokens_used": self.tokens_used,
            "active_seconds": round(self.active_seconds, 1),
            "assessment": dict(self.assessment),
            "reason": self.reason,
            "revision": self.revision,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }

    def budget_left(self) -> int | None:
        """Turns left under the owner's cap, or None without one."""
        turns = self.budget.get("turns")
        return None if not turns else max(0, turns - self.iteration)


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """What one goal turn ended with, as the turn manager saw it."""

    terminal: str  # completed | stopped | cancelled | failed
    error_code: str = ""
    error_message: str = ""
    recoverable: bool = False
    gate: dict[str, Any] | None = None
    tokens: int = 0
    seconds: float = 0.0
    files_changed: int = 0
    checklist_revision: int | None = None


def _clean_budget(raw: Any) -> dict[str, int]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise GoalError("budget must be an object with turns, minutes or tokens")
    budget: dict[str, int] = {}
    for key, value in raw.items():
        if key not in _BUDGET_KEYS:
            raise GoalError(f"unknown budget key {key!r}; use turns, minutes or tokens")
        if value is None or value == 0:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise GoalError(f"budget {key} must be a positive whole number")
        budget[key] = value
    return budget


class GoalService:
    def __init__(self, ctx) -> None:
        self._ctx = ctx
        self._lock = threading.Lock()

    # -- reads -----------------------------------------------------------------

    def get(self, session_id: str) -> Goal | None:
        row = self._ctx.db.query_one(
            "SELECT * FROM session_goals WHERE session_id = ?", (session_id,)
        )
        return _from_row(row) if row is not None else None

    def visible(self, session_id: str) -> Goal | None:
        current = self.get(session_id)
        return None if current is None or current.state == "cleared" else current

    def pursuing(self, session_id: str) -> Goal | None:
        current = self.get(session_id)
        return current if current is not None and current.state == "pursuing" else None

    # -- owner actions ---------------------------------------------------------

    def set(self, session_id: str, objective: str, budget: Any = None) -> Goal:
        text = " ".join(str(objective or "").split()) if objective else ""
        if not text:
            raise GoalError("the goal needs an objective")
        if len(text) > OBJECTIVE_MAX_CHARS:
            raise GoalError(f"the objective is longer than {OBJECTIVE_MAX_CHARS} characters")
        caps = _clean_budget(budget)
        now = now_iso(self._ctx.clock)
        with self._lock:
            current = self.get(session_id)
            revision = (current.revision if current is not None else 0) + 1
            goal = Goal(
                session_id=session_id,
                goal_id=self._ctx.ids.new("goal"),
                objective=text,
                state="pursuing",
                budget=caps,
                iteration=0,
                tokens_used=0,
                active_seconds=0.0,
                quiet_iterations=0,
                assessment={},
                reason="",
                revision=revision,
                started_at=now,
                updated_at=now,
            )
            self._write(goal)
        return goal

    def pause(self, session_id: str, reason: str = "") -> Goal:
        return self._transition(
            session_id, "paused", reason, allowed=("pursuing", "waiting_limits")
        )

    def resume(self, session_id: str) -> Goal:
        current = self._require(session_id)
        if current.state in FINISHED:
            raise GoalError("this goal is over; set a new one with /goal <objective>")
        return self._update(current, state="pursuing", reason="", quiet_iterations=0)

    def clear(self, session_id: str) -> Goal | None:
        current = self.get(session_id)
        if current is None or current.state == "cleared":
            return current
        return self._update(current, state="cleared", reason="")

    def set_budget(self, session_id: str, budget: Any) -> Goal:
        current = self._require(session_id)
        return self._update(current, budget=_clean_budget(budget))

    # -- Rinari's report (goal.update) ------------------------------------------

    def report(
        self,
        session_id: str,
        status: str,
        summary: str,
        evidence: str = "",
        remaining: str = "",
    ) -> Goal:
        if status not in ("progress", "achieved", "blocked"):
            raise GoalError("status must be progress, achieved or blocked")
        if not isinstance(summary, str) or not summary.strip():
            raise GoalError("summary is required: what this iteration did")
        current = self._require(session_id)
        if current.state != "pursuing":
            raise GoalError(f"the goal is {current.state}, not being pursued")
        assessment = {
            "status": status,
            "summary": " ".join(summary.split())[:600],
            "evidence": " ".join(str(evidence or "").split())[:1200],
            "remaining": " ".join(str(remaining or "").split())[:600],
            "iteration": current.iteration,
        }
        return self._update(current, assessment=assessment)

    # -- the turn ended ----------------------------------------------------------

    def begin_iteration(self, session_id: str) -> Goal | None:
        """A goal turn starts: count it. None when there is nothing to pursue."""
        current = self.pursuing(session_id)
        if current is None:
            return None
        return self._update(current, iteration=current.iteration + 1)

    def after_turn(self, session_id: str, outcome: TurnOutcome) -> tuple[Goal | None, bool]:
        """Fold one goal turn into the goal; returns it and whether to go on."""
        current = self.get(session_id)
        if current is None or current.state != "pursuing":
            return current, False
        tokens = current.tokens_used + max(0, outcome.tokens)
        seconds = current.active_seconds + max(0.0, outcome.seconds)
        fields: dict[str, Any] = {"tokens_used": tokens, "active_seconds": seconds}
        assessment = dict(current.assessment)
        reported = assessment.get("iteration") == current.iteration
        if outcome.terminal == "cancelled":
            goal = self._update(current, state="paused", reason="stopped", **fields)
            return goal, False
        if outcome.terminal == "failed":
            limits = outcome.error_code in _LIMIT_CODES
            state = "waiting_limits" if limits else "paused"
            reason = outcome.error_message or outcome.error_code or "the turn failed"
            return self._update(current, state=state, reason=reason[:300], **fields), False
        if reported and assessment.get("status") == "blocked":
            reason = assessment.get("remaining") or assessment.get("summary") or ""
            return self._update(current, state="blocked", reason=reason, **fields), False
        if reported and assessment.get("status") == "achieved":
            gate = outcome.gate or {}
            verdict = gate.get("outcome")
            if verdict and verdict != "DONE":
                # Rinari said done; the verification did not agree. Go on
                # with its reasons.
                assessment["gate"] = {"outcome": verdict, "reasons": gate.get("reasons") or []}
                fields["assessment"] = assessment
            else:
                return self._update(current, state="achieved", reason="", **fields), False
        # Progress: something moved, or the same report again.
        signature = [
            assessment.get("summary") if reported else None,
            outcome.files_changed,
            outcome.checklist_revision,
        ]
        previous = current.assessment.get("_signature")
        quiet = (
            current.quiet_iterations + 1
            if outcome.files_changed == 0 and (not reported or signature == previous)
            else 0
        )
        assessment["_signature"] = signature
        fields["assessment"] = assessment
        fields["quiet_iterations"] = quiet
        if quiet >= STALL_AFTER:
            reason = f"{quiet} iterations in a row without progress"
            return self._update(current, state="stalled", reason=reason, **fields), False
        exhausted = self._exhausted(current, tokens, seconds)
        if exhausted:
            return self._update(current, state="budget_limited", reason=exhausted, **fields), False
        return self._update(current, **fields), True

    def _exhausted(self, goal: Goal, tokens: int, seconds: float) -> str:
        budget = goal.budget
        if budget.get("turns") and goal.iteration >= budget["turns"]:
            return f"the {budget['turns']} turns allowed are used"
        if budget.get("minutes") and seconds >= budget["minutes"] * 60:
            return f"the {budget['minutes']} minutes allowed are used"
        if budget.get("tokens") and tokens >= budget["tokens"]:
            return f"the {budget['tokens']} tokens allowed are used"
        return ""

    # -- what the model reads ------------------------------------------------------

    def render_for_prompt(self, session_id: str) -> str | None:
        """The goal in the task state of every turn while it is pursued."""
        goal = self.pursuing(session_id)
        if goal is None:
            return None
        lines = [f"Goal the user set (pursue it until achieved): {goal.objective}"]
        assessment = goal.assessment
        if assessment.get("summary"):
            lines.append(f"Last report: {assessment.get('status')}: {assessment['summary']}")
        if assessment.get("remaining"):
            lines.append(f"Left: {assessment['remaining']}")
        return "\n".join(lines)

    def continuation(self, goal: Goal) -> str:
        """The harness message that starts the next goal turn."""
        left = goal.budget_left()
        last = left is not None and left <= 1
        parts = [
            f"Runtime: goal, iteration {goal.iteration + 1}"
            + (f" of {goal.budget['turns']}" if goal.budget.get("turns") else "")
            + f". The user's goal: {goal.objective}",
        ]
        assessment = goal.assessment
        if assessment.get("summary"):
            parts.append(f"Your last report: {assessment.get('status')}: {assessment['summary']}")
        if assessment.get("remaining"):
            parts.append(f"You said is left: {assessment['remaining']}")
        gate = assessment.get("gate")
        if gate:
            reasons = "; ".join(str(r) for r in gate.get("reasons") or [])
            parts.append(
                f"You reported it achieved, but the verification said {gate.get('outcome')}"
                + (f": {reasons}" if reasons else "")
                + ". Fix that or record the evidence, then report again."
            )
        if last:
            parts.append(
                "This is the last iteration the user allowed: finish cleanly, leave the work "
                "in a good state, and report with goal.update where it stands and what is left."
            )
        else:
            parts.append(
                "Continue the work toward the goal. When it is achieved, call goal.update with "
                "status achieved, a summary and the evidence. If you cannot go on without the "
                "user, call goal.update with status blocked and say why. Otherwise end the turn "
                "with goal.update status progress: what you did and what is left."
            )
        return "\n".join(parts)

    # -- storage ----------------------------------------------------------------

    def _require(self, session_id: str) -> Goal:
        current = self.get(session_id)
        if current is None or current.state == "cleared":
            raise GoalError("this conversation has no goal")
        return current

    def _transition(self, session_id: str, state: str, reason: str, *, allowed) -> Goal:
        current = self._require(session_id)
        if current.state not in allowed:
            raise GoalError(f"the goal is {current.state}")
        return self._update(current, state=state, reason=reason)

    def _update(self, current: Goal, **changes: Any) -> Goal:
        with self._lock:
            fresh = self.get(current.session_id) or current
            values = {
                field: getattr(fresh, field)
                for field in Goal.__dataclass_fields__  # type: ignore[attr-defined]
            }
            values.update(changes)
            values["revision"] = fresh.revision + 1
            values["updated_at"] = now_iso(self._ctx.clock)
            goal = Goal(**values)
            self._write(goal)
        return goal

    def _write(self, goal: Goal) -> None:
        self._ctx.db.execute(
            "INSERT INTO session_goals (session_id, goal_id, objective, state, budget_json, "
            "iteration, tokens_used, active_seconds, quiet_iterations, assessment_json, reason, "
            "revision, started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET goal_id = excluded.goal_id, "
            "objective = excluded.objective, state = excluded.state, "
            "budget_json = excluded.budget_json, iteration = excluded.iteration, "
            "tokens_used = excluded.tokens_used, active_seconds = excluded.active_seconds, "
            "quiet_iterations = excluded.quiet_iterations, "
            "assessment_json = excluded.assessment_json, reason = excluded.reason, "
            "revision = excluded.revision, started_at = excluded.started_at, "
            "updated_at = excluded.updated_at",
            (
                goal.session_id,
                goal.goal_id,
                goal.objective,
                goal.state,
                json.dumps(goal.budget),
                goal.iteration,
                goal.tokens_used,
                goal.active_seconds,
                goal.quiet_iterations,
                json.dumps(goal.assessment, ensure_ascii=False),
                goal.reason,
                goal.revision,
                goal.started_at,
                goal.updated_at,
            ),
        )


def _from_row(row) -> Goal:
    return Goal(
        session_id=str(row["session_id"]),
        goal_id=str(row["goal_id"]),
        objective=str(row["objective"]),
        state=str(row["state"]),
        budget=json.loads(row["budget_json"] or "{}"),
        iteration=int(row["iteration"]),
        tokens_used=int(row["tokens_used"]),
        active_seconds=float(row["active_seconds"]),
        quiet_iterations=int(row["quiet_iterations"]),
        assessment=json.loads(row["assessment_json"] or "{}"),
        reason=str(row["reason"] or ""),
        revision=int(row["revision"]),
        started_at=str(row["started_at"]),
        updated_at=str(row["updated_at"]),
    )


__all__ = ["GOAL_STATES", "Goal", "GoalError", "GoalService", "TurnOutcome"]
