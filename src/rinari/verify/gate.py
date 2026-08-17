"""Completion gate: turn validation evidence into a completion outcome.

Implements the done-outcome model (AGENTS.md 7):

    DONE | IMPLEMENTED_UNVERIFIED | PARTIAL | BLOCKED | FAILED

and the explicit prohibition of false success: a record that claims
`passed` while its own output shows failures (or shows that nothing ran)
counts as a failure, and "fixed" claims without recorded evidence cannot
become DONE when evidence is required.

The gate is pure: it reads records (dictionaries, as stored) and task
done-when reports; it does not touch storage or the filesystem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

OUTCOME_DONE = "DONE"
OUTCOME_IMPLEMENTED_UNVERIFIED = "IMPLEMENTED_UNVERIFIED"
OUTCOME_PARTIAL = "PARTIAL"
OUTCOME_BLOCKED = "BLOCKED"
OUTCOME_FAILED = "FAILED"

OUTCOMES = (
    OUTCOME_DONE,
    OUTCOME_IMPLEMENTED_UNVERIFIED,
    OUTCOME_PARTIAL,
    OUTCOME_BLOCKED,
    OUTCOME_FAILED,
)

# Markers that reveal a failing run even when the recorder claimed passed.
_FAILURE_MARKERS = (
    re.compile(r"\bFAILED\b"),
    re.compile(r"\b\d+\s+failed\b"),
    re.compile(r"\bfailed=\d+(?![\d.])"),
    re.compile(r"\bTraceback \(most recent call last\)\b"),
    re.compile(r"\bbuild failed\b", re.IGNORECASE),
    re.compile(r"\berror while importing\b", re.IGNORECASE),
)
# "Nothing actually ran" = a pass claim with zero executed units.
_ZERO_RUN_MARKERS = (
    re.compile(r"\bno tests ran\b", re.IGNORECASE),
    re.compile(r"\bno tests collected\b", re.IGNORECASE),
    re.compile(r"\bnothing to do\b"),
    re.compile(r"\b0 passed\b"),
    re.compile(r"\bskipped all\b", re.IGNORECASE),
)


def detect_failure_markers(text: str) -> list[str]:
    markers: list[str] = []
    for pattern in _FAILURE_MARKERS:
        match = pattern.search(text or "")
        if match:
            markers.append(match.group(0).strip())
    return markers


def detect_zero_run(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _ZERO_RUN_MARKERS)


def is_false_success(record: dict) -> bool:
    """A `passed` record whose own output contradicts the claim."""
    if str(record.get("result") or "").lower() != "passed":
        return False
    text = f"{record.get('summary') or ''}\n{record.get('detail') or ''}"
    return bool(detect_failure_markers(text) or detect_zero_run(text))


@dataclass(slots=True)
class GateDecision:
    outcome: str
    reasons: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "evidence": [dict(e) for e in self.evidence],
        }


def evaluate_gate(
    *,
    records: list[dict],
    required_kinds: tuple[str, ...] = ("test",),
    unresolved: tuple[str, ...] = (),
    blocked_reason: str | None = None,
    tasks: list[dict] | None = None,
    require_evidence: bool = True,
) -> GateDecision:
    """Evaluate the latest evidence for each required kind.

    `tasks` is a list of done-when reports (from `done_when_report`) for the
    tasks this completion claim covers; all of them must be satisfiable for
    DONE.
    """
    by_kind = _latest_by_kind(records)
    evidence = [
        {
            "id": rec.get("id"),
            "kind": kind,
            "result": rec.get("result"),
            "summary": rec.get("summary"),
            "false_success": is_false_success(rec),
            "created_at": rec.get("created_at"),
        }
        for kind in required_kinds
        if (rec := by_kind.get(kind)) is not None
    ]

    if blocked_reason:
        return GateDecision(
            outcome=OUTCOME_BLOCKED,
            reasons=[f"blocked: {str(blocked_reason).strip()}"],
            evidence=evidence,
        )

    failed_kinds: list[str] = []
    for kind in required_kinds:
        rec = by_kind.get(kind)
        if rec is None:
            continue
        if str(rec.get("result") or "").lower() in ("failed", "error") or is_false_success(rec):
            detail = ""
            if is_false_success(rec):
                text = f"{rec.get('summary') or ''}\n{rec.get('detail') or ''}"
                markers = detect_failure_markers(text) or (
                    ["nothing ran"] if detect_zero_run(text) else []
                )
                detail = f" false-success markers: {', '.join(markers[:3])}"
            failed_kinds.append(f"{kind}{detail}")
    if unresolved or failed_kinds:
        rs = [f"failed or false-successful: {', '.join(failed_kinds)}"] if failed_kinds else []
        if unresolved:
            rs.append(f"unresolved failures: {', '.join(str(u).strip() for u in unresolved)}")
        return GateDecision(outcome=OUTCOME_FAILED, reasons=rs, evidence=evidence)

    missing = [k for k in required_kinds if by_kind.get(k) is None]
    if require_evidence and missing and not records:
        return GateDecision(
            outcome=OUTCOME_IMPLEMENTED_UNVERIFIED,
            reasons=[
                "no validation evidence recorded; implementation may exist but was not demonstrated"
            ],
            evidence=evidence,
        )

    if missing:
        return GateDecision(
            outcome=OUTCOME_PARTIAL,
            reasons=[f"missing required evidence: {', '.join(missing)}"],
            evidence=evidence,
        )

    task_blockers = _task_blockers(tasks or [])
    if task_blockers:
        return GateDecision(
            outcome=OUTCOME_PARTIAL,
            reasons=[f"task done-when unsatisfied: {', '.join(task_blockers[:6])}"],
            evidence=evidence,
        )

    return GateDecision(
        outcome=OUTCOME_DONE,
        reasons=[f"all required kinds passed: {', '.join(required_kinds)}"],
        evidence=evidence,
    )


def _latest_by_kind(records: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for rec in sorted(records, key=_record_sort_key):
        kind = str(rec.get("kind") or "")
        if kind:
            latest[kind] = rec
    return latest


def _record_sort_key(rec: dict):
    return (str(rec.get("created_at") or ""), str(rec.get("id") or ""))


def _task_blockers(tasks: list[dict]) -> list[str]:
    """Blockers from done-when reports (tasks.core.done_when_report shape)."""
    blockers: list[str] = []
    for task in tasks:
        title = str(task.get("title") or task.get("id") or "task")
        if not task.get("can_complete", True):
            blockers.append(title)
        for group in ("acceptance", "implementation", "validation", "scope"):
            report = task.get(group) or {}
            for text in (report.get("missing") or [])[:2]:
                blockers.append(f"{title} ({group}): {str(text)[:60]}")
        for item in (task.get("unresolved") or [])[:2]:
            blockers.append(f"{title} unresolved: {str(item)[:60]}")
    return blockers
