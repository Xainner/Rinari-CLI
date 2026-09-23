"""Deterministic checks of a compaction summary against recorded state.

A summary is narrative evidence. It does not settle operational facts: whether
work is pending and whether tests passed come from the task and validation
records (`ContextService.build_evidence`), which the compact state carries
separately. These checks catch a summary that contradicts those records, or
that states them with no record behind it. They are not a proof of semantic
fidelity: a summary can pass them and still omit something.
"""

from __future__ import annotations

import re

from rinari.context.compact_state import CompactState

_NOTHING_PENDING = re.compile(
    "|".join(
        (
            r"\bno (?:more )?(?:pending|remaining|outstanding) (?:work|tasks?|items?)\b",
            r"\bnothing (?:is )?(?:left|pending|remaining|outstanding)\b",
            r"\b(?:all|every)(?: of the| the)? (?:tasks?|work|steps?)"
            r"(?: is| are| has been| have been)? (?:done|complete|completed|finished)\b",
            r"\beverything(?: is| has been)? (?:done|complete|completed|finished)\b",
            r"\bthe (?:work|task) is (?:done|complete|completed|finished)\b",
            r"\bsin (?:trabajo|tareas?) pendientes?\b",
            r"\bno (?:queda|hay) (?:nada|trabajo|tareas?)(?: pendientes?)?\b",
            r"\bnada pendiente\b",
            r"\btodo(?: est[aá]| ha sido| qued[oó])? "
            r"(?:hecho|completo|completado|terminado|listo)\b",
            r"\b(?:todas las tareas|el trabajo)(?: est[aá]n?| quedaron?)? "
            r"(?:hech[ao]s?|complet[ao]s?|completad[ao]s?|terminad[ao]s?)\b",
        )
    ),
    re.IGNORECASE,
)

_TESTS_PASSED = re.compile(
    "|".join(
        (
            r"\b(?:all )?(?:the )?tests? (?:all )?(?:pass|passed|passing|are passing"
            r"|succeed|succeeded|are green|went green)\b",
            r"\b(?:las |todas las )?pruebas (?:pasan|pasaron|han pasado|en verde"
            r"|est[aá]n en verde)\b",
        )
    ),
    re.IGNORECASE,
)


def _tests(state: CompactState) -> list[str]:
    return [v for v in state.validations if v.startswith("test:")]


def contradictions(summary: str, state: CompactState) -> list[str]:
    """Why the summary cannot be published against this state; empty if it can."""
    reasons: list[str] = []
    if _NOTHING_PENDING.search(summary):
        open_work = [*state.tasks_active, *state.tasks_blocked, *state.blockers]
        if open_work:
            reasons.append(
                "The summary says no work is pending, but the records show open work: "
                + "; ".join(open_work[:5])
                + "."
            )
        elif not state.tasks_completed:
            reasons.append(
                "The summary says the work is complete, but no completed task is recorded."
            )
    if _TESTS_PASSED.search(summary):
        runs = _tests(state)
        if not runs:
            reasons.append("The summary says tests passed, but no test run is recorded.")
        elif not all(run.split(" (")[0].endswith(": passed") for run in runs):
            reasons.append(
                "The summary says tests passed, but the recorded test run is: "
                + "; ".join(runs)
                + "."
            )
    return reasons


def repair_request(reasons: list[str]) -> str:
    return (
        "Your summary was rejected because it contradicts the recorded state. "
        + " ".join(reasons)
        + " Rewrite it without claiming that work is complete, that nothing is pending "
        "or that tests passed; keep every goal, constraint, decision and open item. "
        "Return only the summary."
    )
