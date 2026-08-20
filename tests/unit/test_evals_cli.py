"""CLI coverage for `rinari eval` (Fase 8): list/show/run/report/compare/
history/create/validate, with real (scripted, network-isolated) runs.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from rinari.cli.main import app
from rinari.shared.paths import ENV_HOME

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "rinari-home"))
    monkeypatch.chdir(work)
    return tmp_path, work


def _call(*args):
    return runner.invoke(app, list(args), catch_exceptions=False)


def _run_ids(tmp_path: object) -> list[str]:
    reports_dir = tmp_path / "rinari-home" / "evals" / "runs"
    if not reports_dir.is_dir():
        return []
    return sorted((p.stem for p in reports_dir.glob("run_*.json")), reverse=True)


def test_eval_list(env):
    result = _call("eval", "list")
    assert result.exit_code == 0, result.output
    for suite in ("security", "soul", "trajectory", "coding", "long_horizon", "multi_agent"):
        assert suite in result.output
    assert "security.sandbox_write_outside" in result.output


def test_eval_list_case_and_unknown_suite(env):
    result = _call("eval", "list", "--case")
    assert result.exit_code == 0, result.output
    assert "security.force_push_denied" in result.output.split()

    bad = _call("eval", "list", "--suite", "nope")
    assert bad.exit_code == 2


def test_eval_show(env):
    result = _call("eval", "show", "security.sandbox_write_outside")
    assert result.exit_code == 0, result.output
    assert "sandbox_write_outside" in result.output

    missing = _call("eval", "show", "nope.nope")
    assert missing.exit_code == 7


def test_eval_run_single_case_report_and_history(env, tmp_path):
    result = _call("eval", "run", "security", "--case", "force_push_denied")
    assert result.exit_code == 0, result.output
    assert "security.force_push_denied" in result.output
    assert "passed" in result.output

    ids = _run_ids(tmp_path)
    assert len(ids) == 1

    history = _call("eval", "history")
    assert history.exit_code == 0, history.output
    assert ids[0] in history.output

    report = _call("eval", "report")
    assert report.exit_code == 0, report.output
    assert "security.force_push_denied" in report.output
    assert "passed" in report.output


def test_eval_run_unknown_case(env):
    result = _call("eval", "run", "security", "--case", "nope")
    assert result.exit_code == 7


def test_eval_compare_two_runs(env, tmp_path):
    first = _call("eval", "run", "security", "--case", "secret_redacted")
    assert first.exit_code == 0, first.output
    after_first = _run_ids(tmp_path)
    second = _call("eval", "run", "security", "--case", "secret_redacted")
    assert second.exit_code == 0, second.output
    # newest first; the newer run is the baseline of comparison "a -> b"
    newer, older = _run_ids(tmp_path)[:2]
    assert newer in after_first or older in after_first

    same = _call("eval", "compare", newer, older)
    assert same.exit_code == 0, same.output
    assert "->" in same.output

    json_res = _call("--json", "eval", "compare", newer, older)
    assert json_res.exit_code == 0, json_res.output
    data = json.loads(json_res.output)
    assert data["data"]["regressions"] == []


def test_eval_create_and_validate(env, tmp_path):
    created = _call("eval", "create", "my_case", "--suite", "custom", "--out", str(tmp_path))
    assert created.exit_code == 0, created.output
    path = tmp_path / "my_case.py"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "EvalCase" in text and "custom.my_case" in text

    validation = _call("eval", "validate", str(path))
    assert validation.exit_code == 0, validation.output
    assert "1 valid case" in validation.output

    # A broken file is reported as invalid (exit 1), not a crash.
    broken = tmp_path / "broken.py"
    broken.write_text("def cases():\n    return []\n", encoding="utf-8")
    bad = _call("eval", "validate", str(broken))
    assert bad.exit_code == 1
    assert "error" in bad.output.lower()


def test_eval_run_external_failing_case(env, tmp_path):
    case_file = tmp_path / "failing_case.py"
    case_file.write_text(
        "from __future__ import annotations\n"
        "from rinari.evals.scripted import answer\n"
        "from rinari.evals.spec import EvalCase, AssertionOutcome, AssertionResult\n"
        "\n"
        "CASES = [\n"
        "    EvalCase(\n"
        '        case_id="custom.fail_probe",\n'
        '        suite="custom",\n'
        '        name="fail_probe",\n'
        '        description="intentional failure for exit-code coverage",\n'
        '        prompt="nope",\n'
        "        project=False,\n"
        "        script=lambda f: [answer('hello')],\n"
        "        expectations=lambda run: [\n"
        '            AssertionResult("wrong", AssertionOutcome.FAILED, "this must fail")\n'
        "        ],\n"
        "    ),\n"
        "]\n",
        encoding="utf-8",
    )
    result = _call("eval", "run", "--file", str(case_file))
    assert result.exit_code == 1, result.output
    assert "failed" in result.output
