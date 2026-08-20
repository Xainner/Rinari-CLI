"""Rinari eval runtime: deterministic, network-isolated evaluation suites.

An eval case drives the real harness (policy, sandbox, tools, loop detection,
compaction, completion gate) with a scripted model caller, then asserts on the
observed trajectory. No case may require the public network: cases that need a
model judge use the configured provider (or are reported as skipped).
"""

from .runner import EvalResult, RunSummary, run_case, run_suite
from .spec import AssertionOutcome, AssertionResult, EvalCase

__all__ = [
    "AssertionOutcome",
    "AssertionResult",
    "EvalCase",
    "EvalResult",
    "RunSummary",
    "run_case",
    "run_suite",
]
