"""Turns that act on a finished plan: implement it or adjust it (plan 0.4.0 #55).

The desktop no longer sends «Implement the plan…» as if the owner typed it.
It starts the turn with an origin that names the plan turn; the Engine checks
that turn is a finished PLAN turn of the same conversation, writes the
model's instruction from the plan itself and, to implement, seeds the turn's
checklist with the plan's numbered steps. The chat shows a separator from the
origin instead of a user bubble.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from rinari.checklist.service import CHECKLIST_MAX_ITEMS
from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError

PLAN_SOURCES = ("plan_implement", "plan_adjust")
# Top level only: a sub-step is indented under its parent (3+ spaces).
_STEP = re.compile(r"^ {0,2}(\d{1,3})[.)]\s+(.+?)\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_MARKUP = re.compile(r"(\*\*|__|`)")


@dataclass(frozen=True, slots=True)
class PlanTurn:
    turn_id: str
    content: str
    title: str


def _payload(row: Any) -> dict[str, Any]:
    try:
        value = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def find_plan(db: Any, session_id: str, plan_turn_id: str) -> PlanTurn:
    """The finished PLAN turn `plan_turn_id` of this session, or INVALID_PARAMS."""
    rows = db.query(
        "SELECT type, payload_json FROM session_events WHERE session_id = ? AND turn_id = ? "
        "AND type IN ('turn.started', 'turn.completed', 'model.content.completed') "
        "ORDER BY seq",
        (session_id, plan_turn_id),
    )
    started = next((_payload(r) for r in rows if r["type"] == "turn.started"), None)
    if started is None:
        raise EngineProtocolError(
            INVALID_PARAMS,
            f"Turn {plan_turn_id} is not a turn of session {session_id}.",
            details={"plan_turn_id": plan_turn_id},
        )
    if started.get("mode") != "plan":
        raise EngineProtocolError(
            INVALID_PARAMS,
            f"Turn {plan_turn_id} is not a PLAN turn.",
            details={"plan_turn_id": plan_turn_id},
        )
    completed = next((_payload(r) for r in rows if r["type"] == "turn.completed"), None)
    if completed is None:
        raise EngineProtocolError(
            INVALID_PARAMS,
            f"The plan of turn {plan_turn_id} did not finish.",
            details={"plan_turn_id": plan_turn_id},
        )
    content = completed.get("content") if isinstance(completed.get("content"), str) else ""
    if not content.strip():
        finals = [
            _payload(r)
            for r in rows
            if r["type"] == "model.content.completed" and _payload(r).get("output_kind") == "final"
        ]
        content = next((p["text"] for p in reversed(finals) if isinstance(p.get("text"), str)), "")
    if not content.strip():
        raise EngineProtocolError(
            INVALID_PARAMS,
            f"The plan of turn {plan_turn_id} has no text.",
            details={"plan_turn_id": plan_turn_id},
        )
    return PlanTurn(turn_id=plan_turn_id, content=content, title=plan_title(content))


def plan_title(content: str) -> str:
    """The plan's first heading, or its first line."""
    for line in content.splitlines():
        heading = _HEADING.match(line)
        if heading:
            return _clean(heading.group(1))[:120]
    first = next((line.strip() for line in content.splitlines() if line.strip()), "")
    return _clean(first)[:120]


def plan_steps(content: str) -> list[str]:
    """The plan's numbered steps, in order (top-level items of numbered lists).

    A plan with a «Steps»/«Pasos» section uses only that section's list, so a
    numbered list of risks or tests elsewhere does not become work items.
    """
    lines = content.splitlines()
    section: list[str] | None = None
    for index, line in enumerate(lines):
        heading = _HEADING.match(line)
        if heading and re.search(
            r"\b(pasos|steps|implementaci[oó]n|implementation)\b", heading.group(1), re.IGNORECASE
        ):
            end = next(
                (j for j in range(index + 1, len(lines)) if _HEADING.match(lines[j])),
                len(lines),
            )
            section = lines[index + 1 : end]
            break
    steps = [_clean(m.group(2)) for line in (section or lines) if (m := _STEP.match(line))]
    return [step[:200] for step in steps if step][:CHECKLIST_MAX_ITEMS]


def checklist_items(steps: list[str]) -> list[dict[str, Any]]:
    return [
        {"id": f"p{index}", "content": step, "status": "pending"}
        for index, step in enumerate(steps, start=1)
    ]


def implement_message(plan: PlanTurn) -> str:
    return (
        "Implement the plan you proposed in the earlier PLAN turn. You are in BUILD mode now: "
        "make the changes, keep the checklist up to date as you go and verify the result. "
        "If two or more steps are real work that can each run on its own (none needs "
        "another's result or touches the same files), leave one followup.suggest with "
        "kind=plan_step per such step except the one you start with (up to 4), say so in "
        "one line, and begin your step at once; a step the user starts as a session is "
        "no longer yours.\n\n"
        "The plan:\n\n" + plan.content
    )


def adjust_message(plan: PlanTurn, comment: str) -> str:
    return (
        "The owner asked to adjust the plan you proposed. Revise that plan with their comment "
        "and answer with the whole revised plan (same structure: title, steps, tests), not only "
        "the differences. Stay in PLAN mode: change nothing yet.\n\n"
        f"Their comment:\n{comment}\n\nThe plan to revise:\n\n{plan.content}"
    )


def parse_origin(raw: Any) -> dict[str, Any] | None:
    """A validated plan origin from `session.turn.start`, or None when absent."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EngineProtocolError(INVALID_PARAMS, "Param 'origin' must be an object.")
    source = raw.get("source")
    if raw.get("kind") != "user" or source not in PLAN_SOURCES:
        raise EngineProtocolError(
            INVALID_PARAMS,
            "Param 'origin' must be {kind: 'user', source: 'plan_implement' | 'plan_adjust', "
            "plan_turn_id, comment?}.",
        )
    plan_turn_id = raw.get("plan_turn_id")
    if not isinstance(plan_turn_id, str) or not plan_turn_id:
        raise EngineProtocolError(INVALID_PARAMS, "Param 'origin.plan_turn_id' is required.")
    origin: dict[str, Any] = {"kind": "user", "source": source, "plan_turn_id": plan_turn_id}
    if source == "plan_adjust":
        comment = raw.get("comment")
        if not isinstance(comment, str) or not comment.strip():
            raise EngineProtocolError(
                INVALID_PARAMS, "Param 'origin.comment' is required to adjust a plan."
            )
        origin["comment"] = comment.strip()
    return origin


def _clean(text: str) -> str:
    return " ".join(_MARKUP.sub("", text).split())
