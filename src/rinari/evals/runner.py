"""Eval runner: executes cases against the real harness with scripted models."""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rinari.shared.errors import RinariError

from .fixtures import EvalFixture, EvalRun, make_clock
from .scripted import ScriptExhaustedError
from .spec import AssertionOutcome, AssertionResult, EvalCase


@dataclass
class EvalResult:
    case_id: str
    suite: str
    name: str
    status: str  # passed | failed | error | skipped
    duration_ms: float
    assertions: list[AssertionResult] = field(default_factory=list)
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "suite": self.suite,
            "name": self.name,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
            "meta": self.meta,
            "assertions": [
                {"name": a.name, "outcome": a.outcome.value, "detail": a.detail}
                for a in self.assertions
            ],
        }


class EvalUnavailableError(RinariError):
    pass

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message, hint=hint)


def run_case(
    case: EvalCase,
    *,
    base_dir: Path,
    clock=None,
) -> EvalResult:
    started = time.monotonic()
    fixture = EvalFixture(case=case, base_dir=base_dir, clock=clock or make_clock()).build()
    run = EvalRun(case_id=case.case_id)
    error = None
    try:
        from rinari.cli.agent_runtime import run_turn

        results = []
        prompts = case.all_prompts()
        for index, prompt in enumerate(prompts):
            if index > 0 and case.resume_between_turns:
                fixture.session.close()
                fixture.session = fixture.open_session()
            if prompt:
                results.append(run_turn(fixture.session, prompt))
        run.turns = results
        run.events = fixture.events()
        run.tools = fixture.tools()
        run.changed_files = fixture.changed_files
        run.fixture = fixture
    except ScriptExhaustedError as exc:
        error = f"script exhausted: {exc}"
    except RinariError as exc:
        error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"

    if error is None:
        try:
            assertions = list(case.expectations(run))
        except Exception as exc:
            assertions = [
                AssertionResult("expectations", AssertionOutcome.FAILED, f"crashed: {exc}")
            ]
    else:
        assertions = [
            AssertionResult("runtime", AssertionOutcome.FAILED, error or "unknown runtime error")
        ]

    status = (
        "error"
        if error
        else (
            "passed"
            if all(
                a.outcome is AssertionOutcome.PASSED or a.outcome is AssertionOutcome.SKIPPED
                for a in assertions
            )
            else "failed"
        )
    )
    if any(a.outcome is AssertionOutcome.SKIPPED for a in assertions) and status == "passed":
        status = "skipped"

    duration = (time.monotonic() - started) * 1000
    try:
        fixture.close()
    finally:
        import shutil

        for sub in (fixture.home, fixture.work):
            if sub is not None and sub.exists():
                shutil.rmtree(sub, ignore_errors=True)
    return EvalResult(
        case_id=case.case_id,
        suite=case.suite,
        name=case.name,
        status=status,
        duration_ms=duration,
        assertions=assertions,
        error=error,
        meta={
            "model_requests": getattr(fixture.model, "request_count", None),
            "tool_calls": len(run.tools),
            "changed_files": run.changed_files,
        },
    )


@dataclass
class RunSummary:
    run_id: str
    results: list[EvalResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")

    @property
    def error(self) -> int:
        return sum(1 for r in self.results if r.status == "error")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.error,
            "skipped": self.skipped,
            "results": [r.to_dict() for r in self.results],
        }


def run_suite(
    suite: tuple[EvalCase, ...],
    *,
    base_dir: Path,
    run_id: str,
    only_case: str | None = None,
    fail_fast: bool = False,
    progress=None,
) -> RunSummary:
    summary = RunSummary(run_id=run_id)
    for case in suite:
        if only_case is not None and case.case_id != only_case:
            continue
        if progress is not None:
            progress(case)
        result = run_case(case, base_dir=base_dir)
        summary.results.append(result)
        if fail_fast and result.status in ("failed", "error"):
            break
    return summary


def make_run_id(now: str | None = None) -> str:
    from rinari.evals.reports import new_run_id

    return new_run_id()
