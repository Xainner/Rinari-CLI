"""Eval case and suite specifications (harness.md observability: evals)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AssertionOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class AssertionResult:
    name: str
    outcome: AssertionOutcome
    detail: str = ""


@dataclass(frozen=True)
class EvalCase:
    """One deterministic evaluation case.

    Attributes:
        suite: logical suite (soul, coding, trajectory, security, long_horizon,
            multi_agent, migration).
        prompt: the user message that starts the (first) turn.
        profile: permission profile for the main session.
        project: git-init the fixture work dir (PROJECT session) when True.
        window: max_context_tokens advertised by the eval model (compaction
            tests); None = the default window.
        config_overrides: dotted config keys written to the user config layer.
        setup: builds fixture files/state; receives the EvalFixture.
        script: returns the scripted ModelResponse sequence; receives the
            EvalFixture (so scripts can read fixture paths).
        expectations: receives the EvalRun and returns AssertionResults.
    """

    case_id: str
    suite: str
    name: str
    description: str
    prompt: str = "Proceed."
    prompts: tuple[str, ...] = ()
    profile: str = "workspace"
    project: bool = True
    window: int | None = None
    config_overrides: dict[str, Any] = field(default_factory=dict)
    resume_between_turns: bool = False
    setup: Callable | None = None
    script: Callable | None = None
    script_lanes: Callable | None = None
    script_route: Callable | None = None
    expectations: Callable = lambda run: [
        AssertionResult("trivial", AssertionOutcome.PASSED),
    ]

    def script_responses(self, fixture):
        if self.script is None:
            return []
        return self.script(fixture)

    def all_prompts(self) -> tuple[str, ...]:
        return self.prompts if self.prompts else (self.prompt,)


@dataclass(frozen=True)
class SuiteSpec:
    key: str
    title: str
    cases: tuple[EvalCase, ...]


def case(case_id: str, suite: str, name: str, **kwargs) -> EvalCase:
    """Small constructor so case definitions read compactly."""
    return EvalCase(case_id=case_id, suite=suite, name=name, **kwargs)
