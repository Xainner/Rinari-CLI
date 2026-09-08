from pathlib import Path

import pytest

from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PolicyEngine, normalize_profile
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.shared.errors import CancelledError
from rinari.shared.redaction import Redactor
from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_NONE,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolResult,
)
from rinari.tools.native import all_native_tools
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime
from rinari.tools.schema import validate_against


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("hello\nworld\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    return tmp_path, root, outside


def _sandbox(project, root, write=True):
    write_roots = (root,) if write else ()
    return FilesystemSandbox(root, write_roots=write_roots)


def _ctx(project, root, *, profile="workspace", write=True, kind="PROJECT", cancel=None):
    tmp_path = project
    return ToolContext(
        session_id="s1",
        kind=kind,
        cwd=root,
        project_root=root if kind == "PROJECT" else None,
        user_home=Path.home(),
        profile=normalize_profile(profile),
        sandbox=_sandbox(project, root, write=write),
        limits=ProcessLimits(timeout_s=5.0, max_output_bytes=1024 * 1024),
        artifact_root=tmp_path / "artifacts",
        clock=FakeClock(start=1_700_000_000.0, step=0.1),
        cancellation=cancel or CancellationToken(),
    )


def _runtime(ctx, tmp_path, *, answer="n", secrets=()):
    registry = ToolRegistry()
    registry.register_all(all_native_tools())
    approvals = ApprovalEngine(prompt=lambda req: answer)
    events: list[tuple[str, dict]] = []
    runtime = ToolRuntime(
        registry,
        PolicyEngine(),
        approvals,
        clock=FakeClock(start=1_700_000_000.0, step=0.1),
        redactor=Redactor(list(secrets)),
        event_sink=lambda t, p: events.append((t, p)),
        spill_threshold_bytes=64 * 1024,
    )
    return runtime, events


# -- schema validator ------------------------------------------------------


def test_schema_validations() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "n": {"type": "integer", "minimum": 1}},
        "required": ["a"],
        "additionalProperties": False,
    }
    assert validate_against(schema, {"a": "x", "n": 2}) == []
    errors = validate_against(schema, {"n": 2, "extra": 1})
    assert any("required" in e for e in errors)
    assert any("unexpected property" in e for e in errors)
    assert validate_against({"type": "object"}, {"a": "str-wrong"}) == []
    assert validate_against(
        {"type": "object", "properties": {"a": {"type": "integer"}}}, {"a": "x"}
    )
    assert validate_against({"type": "string"}, 42)
    assert validate_against({"type": "integer"}, True)
    assert validate_against({"enum": ["a", "b"]}, "c")
    assert validate_against({"type": "array", "items": {"type": "string"}}, ["a", 1])
    assert validate_against({"type": "integer", "minimum": 5}, 3)


# -- registry ------------------------------------------------------------------


def test_registry_search_and_manifest() -> None:
    registry = ToolRegistry()
    registry.register_all(all_native_tools())
    found = registry.search("search text files")
    assert any(t.name == "fs.search_text" for t in found)
    schemas = registry.for_model()
    assert {s.name for s in schemas} == set(registry.names())
    manifest = registry.manifests()
    names = [n["name"] for n in manifest["namespaces"]]
    assert "fs" in names and "shell" in names and "git" in names
    described = registry.describe("fs.read")
    assert described["risk"] == "low"
    assert described["input_schema"]["required"] == ["path"]


# -- pipeline: validation & policy -------------------------------------------


def test_unknown_tool(project) -> None:
    tmp_path, root, _ = project
    runtime, _ = _runtime(_ctx(tmp_path, root), tmp_path)
    result = runtime.execute("nope.tool", {}, _ctx(tmp_path, root))
    assert result.ok is False
    assert result.error.code is ToolErrorCode.TOOL_NOT_FOUND


def test_invalid_arguments(project) -> None:
    tmp_path, root, _ = project
    runtime, _ = _runtime(_ctx(tmp_path, root), tmp_path)
    result = runtime.execute("fs.read", {"wrong": 1}, _ctx(tmp_path, root))
    assert result.ok is False
    assert result.error.code is ToolErrorCode.INVALID_ARGUMENT


def test_policy_deny_read_only_write(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root, profile="read-only", write=False)
    runtime, events = _runtime(ctx, tmp_path)
    result = runtime.execute("fs.write", {"path": "src/new.py", "content": "x"}, ctx)
    assert result.ok is False
    assert result.error.code is ToolErrorCode.POLICY_DENIED
    assert not (root / "src" / "new.py").exists()
    decisions = [p["action"] for t, p in events if t == "PolicyDecision"]
    assert "deny" in decisions


def test_policy_deny_shell_in_read_only(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root, profile="read-only")
    runtime, _ = _runtime(ctx, tmp_path)
    result = runtime.execute("shell.exec", {"command": "echo hi"}, ctx)
    assert result.ok is False
    assert result.error.code is ToolErrorCode.POLICY_DENIED


# -- pipeline: approvals ----------------------------------------------------------


def test_approval_write_outside_root_granted(project) -> None:
    tmp_path, root, _ = project
    (tmp_path / "grantme").mkdir()
    ctx = _ctx(tmp_path, root)
    runtime, events = _runtime(ctx, tmp_path, answer="y")
    result = runtime.execute("fs.write", {"path": "../grantme/file.txt", "content": "ok"}, ctx)
    assert result.ok is True
    assert result.data["bytes_written"] == 2
    assert (tmp_path / "grantme" / "file.txt").read_text(encoding="utf-8") == "ok"
    types = [t for t, _ in events]
    assert "ToolApproved" in types


def test_approval_denied(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path, answer="n")
    result = runtime.execute("fs.write", {"path": "../nope.txt", "content": "x"}, ctx)
    assert result.ok is False
    assert result.error.code is ToolErrorCode.APPROVAL_DENIED
    assert not (tmp_path / "nope.txt").exists()


def test_chat_shell_asks(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root, kind="CHAT", profile="workspace")
    runtime, _ = _runtime(ctx, tmp_path, answer="y")
    result = runtime.execute("shell.exec", {"command": "echo chat-ok"}, ctx)
    assert result.ok is True
    assert "chat-ok" in result.data["stdout"].replace("\r", "")


def test_symlink_escape_asks_denied(project) -> None:
    import os

    tmp_path, root, outside = project
    try:
        os.symlink(outside, root / "link.txt")
    except OSError as exc:
        pytest.skip(f"symlinks not permitted: {exc}")
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path, answer="n")
    result = runtime.execute("fs.read", {"path": "link.txt"}, ctx)
    assert result.ok is False
    assert result.error.code is ToolErrorCode.APPROVAL_DENIED


# -- pipeline: redaction, spill, events, cancellation -----------------------------


def test_redaction_and_events(project) -> None:
    tmp_path, root, _ = project
    root.joinpath("big.py").write_text("data" * 40000, encoding="utf-8")  # > 64KB
    ctx = _ctx(tmp_path, root)
    registry = ToolRegistry()
    registry.register_all(all_native_tools())
    registry.register(
        ToolDefinition(
            name="echo.secret",
            description="test",
            input_schema={"type": "object"},
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=lambda i, c: ToolResult(ok=True, data="key=sk-live-abc123xyz\npayload"),
            namespace="test",
        )
    )
    events: list[tuple[str, dict]] = []
    runtime = ToolRuntime(
        registry,
        PolicyEngine(),
        ApprovalEngine(prompt=lambda r: "y"),
        clock=FakeClock(start=1_700_000_000.0, step=0.1),
        redactor=Redactor(["sk-live-abc123xyz"]),
        event_sink=lambda t, p: events.append((t, p)),
    )
    result = runtime.execute(
        "echo.secret", {}, ctx, tool_call_id="call-1", trace={"turn_index": 2, "tool_seq": 3}
    )
    assert result.ok is True
    assert "sk-live-abc123xyz" not in result.data
    assert "[REDACTED]" in result.data
    types = [t for t, _ in events]
    assert "ToolRequested" in types and "ToolCompleted" in types
    completed = next(payload for kind, payload in events if kind == "ToolCompleted")
    assert completed["tool_call_id"] == "call-1"
    assert completed["turn_index"] == 2
    assert completed["tool_seq"] == 3

    spilled = runtime.execute("fs.read", {"path": "big.py"}, ctx)
    assert spilled.ok is True
    assert spilled.truncated is True
    assert spilled.data["artifact"].startswith("artifact://s1/runtime/")
    assert "spill_guidance" in spilled.data
    assert "text" not in spilled.data
    spill_files = list((tmp_path / "artifacts" / "s1" / "runtime").glob("*.txt"))
    assert len(spill_files) == 1
    assert spill_files[0].stat().st_size > 64 * 1024


def test_cancelled_before_execution(project) -> None:
    tmp_path, root, _ = project
    token = CancellationToken()
    token.cancel()
    ctx = _ctx(tmp_path, root, cancel=token)
    runtime, _ = _runtime(ctx, tmp_path)
    with pytest.raises(CancelledError):
        runtime.execute("fs.read", {"path": "src/a.py"}, ctx)


def test_to_model_text_truncation() -> None:
    result = ToolResult(ok=True, data={"x": "y" * 5000}, tool_call_id="t1")
    text = result.to_model_text()
    assert "[output truncated]" in text


def test_to_model_text_error_envelope() -> None:
    from rinari.tools.definition import ToolErrorCode, ToolErrorInfo

    result = ToolResult(
        ok=False,
        error=ToolErrorInfo(
            code=ToolErrorCode.TIMEOUT, message="request timed out", retryable=True
        ),
        tool_call_id="t9",
    )
    import json as _json

    envelope = _json.loads(result.to_model_text("web.fetch"))
    assert envelope["ok"] is False
    assert envelope["tool"] == "web.fetch"
    assert envelope["error"] == {
        "code": "TIMEOUT",
        "message": "request timed out",
        "retryable": True,
    }
    assert "truncated" not in envelope

    ok_result = ToolResult(ok=True, data={"path": "a.txt"}, tool_call_id="t1")
    ok_envelope = _json.loads(ok_result.to_model_text("fs.stat"))
    assert ok_envelope == {"ok": True, "tool": "fs.stat", "data": {"path": "a.txt"}}


# -- fs tools ---------------------------------------------------------------------


def test_fs_roundtrip(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    ok = runtime.execute("fs.write", {"path": "src/new.py", "content": "print(1)\n"}, ctx)
    assert ok.ok and ok.data["path"].endswith("new.py")

    read = runtime.execute("fs.read", {"path": "src/new.py"}, ctx)
    assert read.ok and read.data["text"].replace("\r\n", "\n") == "print(1)\n"

    patched = runtime.execute(
        "fs.patch", {"path": "src/new.py", "old_string": "print(1)", "new_string": "print(2)"}, ctx
    )
    assert patched.ok and patched.data["replacements"] == 1

    runtime.execute("fs.write", {"path": "multi.txt", "content": "x\nx\nx\n"}, ctx)
    ambiguous = runtime.execute(
        "fs.patch", {"path": "multi.txt", "old_string": "x", "new_string": "y"}, ctx
    )
    assert ambiguous.ok is False
    assert ambiguous.error.code is ToolErrorCode.CONFLICT

    replaced = runtime.execute(
        "fs.patch",
        {"path": "multi.txt", "old_string": "x", "new_string": "y", "replace_all": True},
        ctx,
    )
    assert replaced.ok and replaced.data["replacements"] == 3

    missing = runtime.execute("fs.read", {"path": "nope.py"}, ctx)
    assert missing.ok is False and missing.error.code is ToolErrorCode.NOT_FOUND


def test_fs_list_glob_search_stat_diff(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    listing = runtime.execute("fs.list", {"path": "src"}, ctx)
    assert listing.ok and listing.data["entries"][0]["name"] == "a.py"

    glob = runtime.execute("fs.glob", {"pattern": "**/*.py"}, ctx)
    assert glob.ok and any(p.endswith("a.py") for p in glob.data["matches"])

    found = runtime.execute("fs.search_text", {"pattern": "world"}, ctx)
    assert found.ok and found.data["matches"][0]["lines"][0]["line"] == 2

    stat = runtime.execute("fs.stat", {"path": "src/a.py"}, ctx)
    assert stat.ok and stat.data["size_bytes"] == root.joinpath("src", "a.py").stat().st_size

    root.joinpath("b.py").write_text("hello\nplanet\n", encoding="utf-8")
    diff = runtime.execute("fs.diff", {"a": "src/a.py", "b": "b.py"}, ctx)
    assert diff.ok and "+planet" in diff.data["diff"]


def test_fs_read_lines(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    result = runtime.execute("fs.read_lines", {"path": "src/a.py", "start": 2, "end": 2}, ctx)
    assert result.ok and result.data["lines"] == [{"line": 2, "text": "world"}]


# -- shell tools --------------------------------------------------------------------


def test_shell_exec_ok_and_nonzero(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    result = runtime.execute("shell.exec", {"command": "echo hello-shell"}, ctx)
    assert result.ok is True
    assert "hello-shell" in result.data["stdout"].replace("\r", "")
    assert result.data["exit_code"] == 0

    failure = runtime.execute("shell.exec", {"command": 'python -c "import sys; sys.exit(3)"'}, ctx)
    assert failure.ok is True
    assert failure.data["exit_code"] == 3


def test_shell_timeout(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    command = "ping -n 11 127.0.0.1 >nul" if sys_platform_is_windows() else "sleep 5"
    result = runtime.execute("shell.exec", {"command": command, "timeout_s": 1}, ctx)
    assert result.ok is False
    assert result.error.code is ToolErrorCode.TIMEOUT


def sys_platform_is_windows() -> bool:
    import sys

    return sys.platform == "win32"


# -- git tools ------------------------------------------------------------------------


def test_git_tools_in_repo(project) -> None:
    tmp_path, root, _ = project
    ctx = _ctx(tmp_path, root)
    runtime, _ = _runtime(ctx, tmp_path)
    status = runtime.execute("git.status", {}, ctx)
    # not a git repo -> NOT_FOUND (structured, not a crash)
    if status.ok is False:
        assert status.error.code is ToolErrorCode.NOT_FOUND
    else:
        assert status.data["branch"]

    init_cmd = "git init -q && git config user.email a@b.c && git config user.name t"
    init = runtime.execute("shell.exec", {"command": init_cmd}, ctx)
    assert init.ok is True
    status = runtime.execute("git.status", {}, ctx)
    assert status.ok is True
    assert status.data["dirty"] is True
    log = runtime.execute("git.log", {"limit": 5}, ctx)
    assert log.ok is True
    branch = runtime.execute("git.branch", {}, ctx)
    assert branch.ok is True and branch.data["current"]
