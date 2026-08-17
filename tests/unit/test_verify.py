import os
from pathlib import Path

import pytest

from rinari.policy.engine import CAPABILITY_STATE_READ, CAPABILITY_STATE_WRITE
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.shared.clock import FakeClock
from rinari.shared.errors import InvalidUsageError
from rinari.tools.definition import ToolContext
from rinari.tools.native.verify import verify_tools
from rinari.verify.gate import (
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    OUTCOME_FAILED,
    OUTCOME_IMPLEMENTED_UNVERIFIED,
    OUTCOME_PARTIAL,
    evaluate_gate,
    is_false_success,
)
from rinari.verify.planner import plan_verification
from rinari.verify.service import VerificationService

# -- planner -----------------------------------------------------------------


def test_plan_targeted_via_test_map():
    plan = plan_verification(
        ["src/foo.py", "tests/test_bar.py"],
        test_map={"src/foo.py": ["tests/test_foo.py"]},
    )
    assert plan.targeted == ("tests/test_foo.py", "tests/test_bar.py")
    assert plan.risk == "medium"  # sources + tests
    assert "no test mapping for src/foo.py" not in plan.reasons


def test_plan_broader_on_config_change():
    plan = plan_verification(["pyproject.toml", "src/foo.py"])
    assert plan.broader == ("suite",)
    assert plan.risk == "high"
    assert any("broader suite" in r for r in plan.reasons)


def test_plan_risk_low_single_source():
    plan = plan_verification(["src/foo.py"], test_map={"src/foo.py": ["tests/test_foo.py"]})
    assert plan.risk == "low"
    assert plan.broader == ()


def test_plan_user_constraints_and_instructions():
    plan = plan_verification(
        ["src/foo.py"],
        test_map={"src/foo.py": ["tests/test_foo.py"]},
        user_constraints=["run integration suite too"],
        project_instructions=["Always run `pytest --cov` before finishing changes."],
    )
    assert plan.user_constraints == ("run integration suite too",)
    assert plan.project_instructions == ("Always run `pytest --cov` before finishing changes.",)
    assert any(r.startswith("project instruction:") for r in plan.reasons)


def test_plan_discovered_commands():
    plan = plan_verification(
        ["src/foo.py"],
        test_map={"src/foo.py": ["tests/test_foo.py"]},
        discovered={"test": ["uv run pytest"], "lint": ["uv run ruff check ."], "typecheck": []},
    )
    assert plan.test_commands == ("uv run pytest",)
    assert plan.lint_commands == ("uv run ruff check .",)


# -- completion gate -----------------------------------------------------------


def _record(kind="test", result="passed", i=1, detail="ok output", summary="ok") -> dict:
    return {
        "id": f"val_{i:02d}",
        "kind": kind,
        "result": result,
        "summary": summary,
        "detail": detail,
        "created_at": f"2026-01-01T00:00:{i:02d}Z",
    }


def test_gate_done_when_latest_passes():
    records = [_record("test", "failed", 1), _record("test", "passed", 2)]
    decision = evaluate_gate(records=records)
    assert decision.outcome == OUTCOME_DONE
    assert decision.evidence[0]["result"] == "passed"


def test_gate_failed_on_failed_latest():
    records = [_record("test", "passed", 1), _record("test", "failed", 2)]
    decision = evaluate_gate(records=records)
    assert decision.outcome == OUTCOME_FAILED


def test_gate_rejects_false_success():
    records = [_record("test", "passed", 1, summary="3 failed")]
    assert is_false_success(records[0])
    assert evaluate_gate(records=records).outcome == OUTCOME_FAILED


def test_gate_rejects_zero_run():
    records = [_record("test", "passed", 1, detail="no tests ran in 0.01s")]
    assert evaluate_gate(records=records).outcome == OUTCOME_FAILED


def test_gate_no_evidence_is_unverified():
    assert evaluate_gate(records=[]).outcome == OUTCOME_IMPLEMENTED_UNVERIFIED


def test_gate_partial_when_required_kind_missing():
    records = [_record("test", "passed", 1)]
    decision = evaluate_gate(records=records, required_kinds=("test", "lint"))
    assert decision.outcome == OUTCOME_PARTIAL
    assert "lint" in decision.reasons[0]


def test_gate_blocked_wins():
    records = [_record("test", "passed", 1)]
    decision = evaluate_gate(records=records, blocked_reason="waiting on CI keys")
    assert decision.outcome == OUTCOME_BLOCKED


def test_gate_unresolved_failures_fail():
    records = [_record("test", "passed", 1)]
    assert (
        evaluate_gate(records=records, unresolved=["flake in tests/test_z.py"]).outcome
        == OUTCOME_FAILED
    )


def test_gate_task_done_when_blockers():
    records = [_record("test", "passed", 1)]
    task_report = {
        "title": "ship it",
        "id": "task_1",
        "can_complete": False,
        "acceptance": {"total": 1, "satisfied": 0, "ok": False, "missing": ["e2e passes"]},
        "implementation": {"total": 0, "satisfied": 0, "ok": True, "missing": []},
        "validation": {"total": 1, "satisfied": 1, "ok": True, "missing": []},
        "scope": {"total": 0, "satisfied": 0, "ok": True, "missing": []},
        "unresolved": [],
    }
    decision = evaluate_gate(records=records, tasks=[task_report])
    assert decision.outcome == OUTCOME_PARTIAL
    assert any("ship it" in r for r in decision.reasons)


# -- service + repository ------------------------------------------------------


def test_service_record_and_latest(app_ctx, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    service = VerificationService(app_ctx)
    first = service.record(
        project, kind="test", result="failed", command="pytest", detail="1 failed"
    )
    second = service.record(
        project, kind="test", result="passed", command="pytest", detail="5 passed"
    )
    assert first["id"] != second["id"]
    latest = service.latest(project)
    assert [row["id"] for row in latest] == [second["id"], first["id"]]
    decision = service.evaluate(project)
    assert decision.outcome == OUTCOME_DONE


def test_service_record_rejects_bad_kind(app_ctx, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    service = VerificationService(app_ctx)
    with pytest.raises(InvalidUsageError):
        service.record(project, kind="nonsense", result="passed")


# -- tools -----------------------------------------------------------------------


def _tool_ctx(app_ctx, tmp_path) -> ToolContext:
    root = tmp_path / "proj"
    root.mkdir()
    return ToolContext(
        session_id="ses_test",
        kind="PROJECT",
        cwd=root,
        project_root=root,
        user_home=None,
        profile=None,
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(),
        artifact_root=root / ".artifacts",
        clock=FakeClock(start=1_700_000_000.0, step=1.0),
        validation=VerificationService(app_ctx),
        project_trusted=True,
    )


def _handler(name: str):
    tool = next(t for t in verify_tools() if t.name == name)
    return tool


def test_verify_record_tool(app_ctx, tmp_path):
    ctx = _tool_ctx(app_ctx, tmp_path)
    result = _handler("verify.record").handler(
        {"kind": "test", "result": "passed", "command": "pytest", "detail": "2 passed"}, ctx
    )
    assert result.ok
    assert result.data["kind"] == "test"
    assert result.data["id"].startswith("val_")


def test_verify_record_tool_requires_fields(app_ctx, tmp_path):
    ctx = _tool_ctx(app_ctx, tmp_path)
    result = _handler("verify.record").handler({"kind": "test"}, ctx)
    assert not result.ok
    assert result.error.code.value == "INVALID_ARGUMENT"


def test_verify_plan_and_evaluate_tools(app_ctx, tmp_path):
    ctx = _tool_ctx(app_ctx, tmp_path)
    plan = _handler("verify.plan").handler({"changed_files": ["src/foo.py"]}, ctx)
    assert plan.ok
    assert plan.data["targeted"]
    _handler("verify.record").handler(
        {"kind": "test", "result": "passed", "command": "pytest", "detail": "2 passed"}, ctx
    )
    decision = _handler("verify.evaluate").handler({"required_kinds": ["test"]}, ctx)
    assert decision.ok
    assert decision.data["outcome"] == OUTCOME_DONE


def test_verify_capabilities(app_ctx, tmp_path):
    ctx = _tool_ctx(app_ctx, tmp_path)
    assert _handler("verify.record").classify({}).capability == CAPABILITY_STATE_WRITE
    assert _handler("verify.plan").classify({}).capability == CAPABILITY_STATE_READ
    assert _handler("verify.evaluate").classify({}).capability == CAPABILITY_STATE_READ
    from rinari.policy.engine import PolicyEngine

    engine = PolicyEngine()
    from rinari.policy.engine import PermissionProfile, SessionScope

    scope = SessionScope(
        kind="PROJECT", root=ctx.cwd, cwd=ctx.cwd, profile=PermissionProfile.WORKSPACE
    )
    assert engine.decide(CAPABILITY_STATE_READ, scope).action.value == "allow"
    assert engine.decide(CAPABILITY_STATE_WRITE, scope).action.value == "allow"
    readonly = SessionScope(
        kind="PROJECT", root=ctx.cwd, cwd=ctx.cwd, profile=PermissionProfile.READ_ONLY
    )
    assert engine.decide(CAPABILITY_STATE_WRITE, readonly).action.value == "deny"


# -- pty -------------------------------------------------------------------------


@pytest.mark.skipif(
    not hasattr(os, "openpty") or not Path("/bin/sh").exists(), reason="POSIX required"
)
def test_pty_roundtrip(tmp_path):
    from rinari.policy.engine import PermissionProfile
    from rinari.tools.native.ptytools import PtyRegistry, pty_tools

    registry = PtyRegistry()
    ctx = ToolContext(
        session_id="ses_pty",
        kind="PROJECT",
        cwd=tmp_path,
        project_root=tmp_path,
        user_home=None,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(read_root=tmp_path, write_roots=(tmp_path,)),
        limits=ProcessLimits(),
        artifact_root=tmp_path / ".artifacts",
        clock=FakeClock(start=1_700_000_000.0, step=1.0),
        pty=registry,
    )
    by_name = {tool.name: tool.handler for tool in pty_tools()}
    started = by_name["pty.start"]({"command": "cat"}, ctx)
    assert started.ok
    handle = started.data["handle"]
    written = by_name["pty.write"]({"handle": handle, "data": "hello pty"}, ctx)
    assert written.ok
    import time

    deadline = time.monotonic() + 5.0
    output = ""
    while time.monotonic() < deadline:
        read = by_name["pty.read"]({"handle": handle, "timeout_s": 0.5}, ctx)
        assert read.ok
        output = read.data["output"]
        if "hello pty" in output:
            break
    assert "hello pty" in output
    terminated = by_name["pty.terminate"]({"handle": handle}, ctx)
    assert terminated.ok


@pytest.mark.skipif(hasattr(os, "openpty"), reason="only meaningful on non-POSIX")
def test_pty_unavailable_outside_posix(tmp_path):

    from rinari.tools.native.ptytools import pty_tools

    ctx = ToolContext(
        session_id="ses_pty",
        kind="CHAT",
        cwd=tmp_path,
        project_root=None,
        user_home=None,
        profile=None,
        sandbox=FilesystemSandbox(read_root=tmp_path),
        limits=ProcessLimits(),
        artifact_root=tmp_path,
        clock=FakeClock(start=1_700_000_000.0, step=1.0),
    )
    started = next(t for t in pty_tools() if t.name == "pty.start").handler(
        {"command": "echo hi"}, ctx
    )
    assert not started.ok
    assert started.error.code.value == "DEPENDENCY_ERROR"
