"""Model judge support (harness.md: evals / model judge).

Judges score generated text against a rubric. The default judge is
deterministic (regex/keyword rules) so suites stay network-isolated; the
model judge calls the *configured* provider and is only used when a case
opts in and a non-eval provider is available.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum


class JudgeOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class JudgeResult:
    outcome: JudgeOutcome
    score: float | None
    rationale: str


@dataclass(frozen=True)
class Rule:
    """One deterministic rubric rule: pattern that must (or must not) appear."""

    pattern: str
    required: bool = True
    flags: int = re.IGNORECASE


class RuleJudge:
    """Deterministic judge: every `required` rule must match, every optional
    rule must not match."""

    def __init__(self, rules: list[Rule]):
        self.rules = rules

    def judge(self, text: str) -> JudgeResult:
        failures = []
        for rule in self.rules:
            found = re.search(rule.pattern, text, rule.flags) is not None
            if rule.required and not found:
                failures.append(f"missing {rule.pattern!r}")
            if not rule.required and found:
                failures.append(f"forbidden {rule.pattern!r}")
        if failures:
            return JudgeResult(JudgeOutcome.FAIL, None, "; ".join(failures))
        return JudgeResult(JudgeOutcome.PASS, 1.0, "all rules satisfied")


def parse_judge_json(raw: str) -> dict:
    """Extract the JSON verdict from a model-judge reply (tolerant of fences)."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match is None:
        raise ValueError("judge reply has no JSON object")
    return json.loads(match.group(0))


def model_judge_prompt(rubric: str, text: str) -> str:
    return (
        "You are an impartial eval judge. Score the assistant output against "
        "the rubric. Reply with ONLY a JSON object: "
        '{"pass": boolean, "score": number between 0 and 1, "rationale": string}.\n\n'
        f"Rubric:\n{rubric}\n\nAssistant output:\n{text}"
    )
