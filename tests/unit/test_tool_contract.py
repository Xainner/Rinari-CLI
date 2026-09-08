"""Etapa C — tool contract enforcement: output schema, output caps, retries, scheduler."""

from __future__ import annotations

import pytest

from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PolicyEngine, normalize_profile
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.shared.redaction import Redactor
from rinari.tools.definition import (
    SIDE_EFFECT_LOCAL_REVERSIBLE,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime
from rinari.tools.scheduler import schedule


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    return tmp_path, root


def _ctx(tmp_path, root):
    return ToolContext(
        session_id="s1",
        kind="PROJECT",
        cwd=root,
        project_root=root,
        user_home=tmp_path,
        profile=normalize_profile("workspace"),
        sandbox=FilesystemSandbox(root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=5.0, max_output_bytes=1024 * 1024),
        artifact_root=tmp_path / "artifacts",
        clock=FakeClock(start=1_700_000_000.0, step=0.1),
        cancellation=CancellationToken(),
    )


def _runtime(tools, *, spill=64 * 1024):
    registry = ToolRegistry()
    registry.register_all(tools)
    return ToolRuntime(
        registry,
        PolicyEngine(),
        ApprovalEngine(prompt=lambda req: "n"),
        clock=FakeClock(start=1_700_000_000.0, step=0.1),
        redactor=Redactor(),
        spill_threshold_bytes=spill,
    )


def _def(name, handler, **kwargs):
    # Classify as an in-root fs.read so policy ALLOWs; the handler itself is
    # synthetic and ignores arguments.
    kwargs.setdefault(
        "classify", lambda args: ClassifiedAction("fs.read", str(args.get("path") or ""))
    )
    return ToolDefinition(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {}},
        handler=handler,
        **kwargs,
    )


def _args(root) -> dict:
    return {"path": str(root / "x.txt")}


# -- output_schema ---------------------------------------------------------


def test_output_schema_violation_is_validation_failed(project) -> None:
    tmp_path, root = project
    rt = _runtime(
        [
            _def(
                "mcp.thing",
                lambda args, ctx: ToolResult(ok=True, data={"wrong": "shape"}),
                output_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            )
        ]
    )
    result = rt.execute("mcp.thing", _args(root), _ctx(tmp_path, root), tool_call_id="t1")
    assert not result.ok
    assert result.error is not None and result.error.code == ToolErrorCode.VALIDATION_FAILED


def test_output_schema_ok_passes_and_absent_schema_skips(project) -> None:
    tmp_path, root = project
    rt = _runtime(
        [
            _def(
                "mcp.thing",
                lambda args, ctx: ToolResult(ok=True, data={"value": "x"}),
                output_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            ),
            _def("plain.tool", lambda args, ctx: ToolResult(ok=True, data={"anything": 1})),
        ]
    )
    ctx = _ctx(tmp_path, root)
    assert rt.execute("mcp.thing", _args(root), ctx, tool_call_id="t1").ok
    assert rt.execute("plain.tool", _args(root), ctx, tool_call_id="t2").ok


# -- per-tool output caps ---------------------------------------------------


def test_per_tool_output_cap_below_global(project) -> None:
    tmp_path, root = project
    big = "z" * 4000
    rt = _runtime(
        [_def("small.tool", lambda args, ctx: ToolResult(ok=True, data=big), max_output_bytes=100)],
        spill=64 * 1024,
    )
    result = rt.execute("small.tool", _args(root), _ctx(tmp_path, root), tool_call_id="t1")
    assert result.truncated
    assert any(a.kind == "tool-output" for a in result.artifacts)


# -- idempotent retries ------------------------------------------------------


def test_idempotent_tool_retries_retryable_failure(project) -> None:
    tmp_path, root = project
    attempts = {"n": 0}

    def flaky(args, ctx):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.NETWORK_ERROR, "transient", retryable=True),
            )
        return ToolResult(ok=True, data={"ok": True})

    rt = _runtime([_def("api.flaky", flaky, idempotent=True)])
    result = rt.execute("api.flaky", _args(root), _ctx(tmp_path, root), tool_call_id="t1")
    assert result.ok
    assert attempts["n"] == 2


def test_non_idempotent_tool_never_retries(project) -> None:
    tmp_path, root = project
    attempts = {"n": 0}

    def flaky(args, ctx):
        attempts["n"] += 1
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(ToolErrorCode.NETWORK_ERROR, "transient", retryable=True),
        )

    rt = _runtime(
        [
            _def(
                "api.writer",
                flaky,
                idempotent=False,
                side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            )
        ]
    )
    result = rt.execute("api.writer", _args(root), _ctx(tmp_path, root), tool_call_id="t1")
    assert not result.ok
    assert attempts["n"] == 1


# -- scheduler ----------------------------------------------------------------


def _sched_tool(name, side_effects=SIDE_EFFECT_NONE, **kwargs):
    return ToolDefinition(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {}},
        side_effects=side_effects,
        handler=lambda args, ctx: ToolResult(ok=True, data={}),
        **kwargs,
    )


def test_reads_share_a_group_writes_are_alone() -> None:
    reg = ToolRegistry()
    reg.register_all(
        [
            _sched_tool("fs.read"),
            _sched_tool("fs.list"),
            _sched_tool("fs.write", side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE, idempotent=False),
        ]
    )
    groups = schedule(["fs.read", "fs.list", "fs.write", "fs.read"], reg)
    assert groups[0] == ["fs.read", "fs.list"]
    assert groups[1] == ["fs.write"]
    assert groups[2] == ["fs.read"]


def test_unknown_tools_run_serially_alone() -> None:
    reg = ToolRegistry()
    reg.register_all([_sched_tool("fs.read")])
    groups = schedule(["fs.read", "nope.missing"], reg)
    assert groups == [["fs.read"], ["nope.missing"]]
