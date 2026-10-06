"""Native tools reject arguments they do not declare, before running anything.

A model sent ``timeout_ms: 900000`` to shell.exec, whose argument is
``timeout_s``. The key was accepted and ignored, the command died at the 60 s
default and the model reported a fixed cap that does not exist.
"""

from __future__ import annotations

import dataclasses
import sys
import time

from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_NONE,
    ToolDefinition,
    ToolErrorCode,
    ToolResult,
)
from rinari.tools.native.shell import effective_timeout
from rinari.tools.schema import declared_properties
from tests.unit import test_tool_runtime as base
from tests.unit.test_tool_runtime import _ctx, _runtime

project = base.project


def _counting_tool(name: str, schema: dict, *, source: str | None = None):
    calls: list[dict] = []

    def handler(arguments, ctx):
        calls.append(arguments)
        return ToolResult(ok=True, data={})

    manifest = {"source": source} if source else {}
    tool = ToolDefinition(
        name=name,
        description="test",
        input_schema=schema,
        risk=RISK_LOW,
        side_effects=SIDE_EFFECT_NONE,
        handler=handler,
        manifest=manifest,
    )
    return tool, calls


def test_shell_exec_rejects_timeout_ms_and_names_timeout_s(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root, profile="full-access")
    runtime, _ = _runtime(ctx, tmp_path, answer="y")
    marker = root / "ran.txt"
    command = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"]

    result = runtime.execute("shell.exec", {"argv": command, "timeout_ms": 900000}, ctx)

    assert result.ok is False
    assert result.error.code is ToolErrorCode.INVALID_ARGUMENT
    assert "'timeout_ms'" in result.error.message
    assert "did you mean 'timeout_s'" in result.error.message
    assert "Seconds" in result.error.message
    assert result.error.details["unknown_arguments"] == ["timeout_ms"]
    assert "timeout_s" in result.error.details["accepted"]
    assert not marker.exists()


def test_process_output_rejects_a_wait_timeout(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)

    result = runtime.execute("process.output", {"handle": "proc_1", "timeout_s": 90}, ctx)

    assert result.error.code is ToolErrorCode.INVALID_ARGUMENT
    assert "process.output does not accept parameter 'timeout_s'" in result.error.message


def test_branch_properties_and_request_id_are_accepted(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path, answer="y")
    tool, calls = _counting_tool(
        "fs.branchy",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "oneOf": [
                {"properties": {"text": {"type": "string"}}, "required": ["text"]},
                {"properties": {"lines": {"type": "array"}}, "required": ["lines"]},
            ],
        },
    )
    runtime.registry.register(tool)

    ok = runtime.execute("fs.branchy", {"path": "a", "text": "x"}, ctx)
    bad = runtime.execute("fs.branchy", {"path": "a", "text": "x", "mode": "w"}, ctx)

    assert ok.ok, ok.error
    assert bad.error.code is ToolErrorCode.INVALID_ARGUMENT
    assert len(calls) == 1


def test_open_and_external_schemas_keep_their_own_contract(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path, answer="y")
    open_tool, open_calls = _counting_tool(
        "x.open",
        {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True},
    )
    mcp_tool, mcp_calls = _counting_tool(
        "mcp.srv.echo", {"type": "object", "properties": {"a": {"type": "string"}}}, source="mcp"
    )
    runtime.registry.register(open_tool)
    runtime.registry.register(mcp_tool)

    assert runtime.execute("x.open", {"a": "1", "extra": 2}, ctx).ok
    assert runtime.execute("mcp.srv.echo", {"a": "1", "extra": 2}, ctx).ok
    assert len(open_calls) == len(mcp_calls) == 1


def test_every_native_tool_declares_what_its_handler_reads(project) -> None:
    # Undeclared keys a handler reads would now be rejected; the paginated
    # search tools and the web cache bypass were the ones in use.
    tmp_path, root, _ = project
    runtime, _ = _runtime(_ctx(tmp_path, root), tmp_path)
    for name, keys in {
        "fs.glob": {"limit", "offset"},
        "search.files": {"limit", "offset"},
        "web.find": {"refresh"},
        "web.sources": {"refresh"},
    }.items():
        assert keys <= declared_properties(runtime.registry.get(name).input_schema), name


def test_shell_timeout_reports_its_origin(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root, profile="full-access")
    runtime, _ = _runtime(ctx, tmp_path, answer="y")
    sleep = [sys.executable, "-c", "import time; time.sleep(30)"]

    result = runtime.execute("shell.exec", {"argv": sleep, "timeout_s": 1}, ctx)

    assert result.error.code is ToolErrorCode.TIMEOUT
    assert result.error.message == "Command exceeded timeout_s=1s and was terminated"
    assert result.data["timeout"] == {"requested_s": 1, "effective_s": 1.0, "source": "argument"}


def test_effective_timeout_respects_the_tool_deadline(project) -> None:
    tmp_path, root, _ = project
    base = _ctx(tmp_path, root)

    assert effective_timeout(None, 60.0, base) == {
        "requested_s": None,
        "effective_s": 60.0,
        "source": "default",
    }
    assert effective_timeout(900, 60.0, base)["effective_s"] == 900.0

    near = dataclasses.replace(base, deadline_at=time.time() + 5)
    capped = effective_timeout(900, 60.0, near)
    assert capped["source"] == "deadline" and capped["effective_s"] <= 5
    assert capped["requested_s"] == 900
