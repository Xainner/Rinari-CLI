"""The context continuity suite runs in CI (review plan §E, deterministic half).

Each case drives the real harness through repeated compactions with scripted
models; a failure names the assertion and what the model actually saw.
"""

import pytest

from rinari.evals import builtins, context_cases
from rinari.evals.runner import run_case

CASES = (*context_cases.cases(), builtins.LH_COMPACTION)


@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
def test_context_case_passes(case, tmp_path):
    result = run_case(case, base_dir=tmp_path)
    failed = [(a.name, a.detail) for a in result.assertions if a.outcome.value == "failed"]
    assert result.status == "passed", (result.error, failed)


def test_the_suite_is_registered():
    assert {case.case_id for case in context_cases.cases()} <= {
        case.case_id for case in builtins.suites()["context"]
    }
