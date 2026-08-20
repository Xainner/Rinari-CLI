"""Lifecycle hooks tests (phase 5).

Covers declaration parsing, engine ordering/trust/capability/timeout/failure
isolation, python + shell handlers, HookService discovery/state/test/doctor,
and the agent-loop wiring (BeforeModel/AfterModel/PreToolUse/PostToolUse/
ToolError/BeforeFinal) through a scripted provider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rinari.application.services import build_services
from rinari.hooks.engine import HookDeclaration, HookEngine, HookError
from rinari.hooks.events import ALL_EVENTS

# ---------------------------------------------------------------------------
# Declaration parsing
# ---------------------------------------------------------------------------


def test_parse_ok():
    d = HookDeclaration.parse(
        {"name": "h1", "event": "PostToolUse", "handler_type": "python", "handler": "m:f"},
        "user",
    )
    assert d.key == ("global", "user", "h1")
    assert d.risk == "low"


def test_parse_shell_uses_command():
    d = HookDeclaration.parse(
        {"name": "sh", "event": "PreToolUse", "handler_type": "shell", "command": "echo hi"},
        "user",
    )
    assert d.handler == "echo hi"


@pytest.mark.parametrize(
    "raw,code",
    [
        (
            {"name": "x", "event": "Nope", "handler_type": "shell", "command": "e"},
            "HOOK_EVENT_INVALID",
        ),
        (
            {"name": "x", "event": "PostToolUse", "handler_type": "wasm", "handler": "e"},
            "HOOK_HANDLER_INVALID",
        ),
        (
            {"name": "", "event": "PostToolUse", "handler_type": "shell", "command": "e"},
            "HOOK_HANDLER_INVALID",
        ),
        ({"name": "x", "event": "PostToolUse", "handler_type": "shell"}, "HOOK_HANDLER_INVALID"),
        (
            {
                "name": "x",
                "event": "PostToolUse",
                "handler_type": "python",
                "handler": "m:f",
                "capabilities": "nope",
            },
            "HOOK_HANDLER_INVALID",
        ),
    ],
    ids=["bad-event", "bad-type", "no-name", "no-handler", "bad-caps"],
)
def test_parse_invalid(raw, code):
    with pytest.raises(HookError) as excinfo:
        HookDeclaration.parse(raw, "user")
    assert excinfo.value.code == code


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class RecordingTrust:
    def __init__(self, trusted: bool = True):
        self.trusted = trusted

    def is_trusted(self, path) -> bool:
        return self.trusted


def test_engine_order_is_deterministic():
    engine = HookEngine()
    engine.add(
        HookDeclaration(
            name="zz", event="PostToolUse", handler_type="python", handler="m:z", source="plugin:p"
        )
    )
    engine.add(
        HookDeclaration(
            name="aa", event="PostToolUse", handler_type="python", handler="m:a", source="project"
        )
    )
    engine.add(
        HookDeclaration(
            name="bb", event="PostToolUse", handler_type="python", handler="m:b", source="user"
        )
    )
    matching = [d.name for d in engine.matching("PostToolUse")]
    assert matching == ["bb", "aa", "zz"]  # user < project < plugin, then name


def test_engine_project_requires_trust():
    project = Path("/tmp/proj")
    outcomes = HookEngine(trust=RecordingTrust(trusted=False)).emit(
        "PostToolUse", {}, project=project
    )
    assert outcomes == []  # nothing registered -> none
    decl = HookDeclaration(
        name="ph", event="PostToolUse", handler_type="python", handler="m:h", source="project"
    )
    engine = HookEngine(trust=RecordingTrust(trusted=False))
    engine.add(decl)
    outcomes = engine.emit("PostToolUse", {}, project=project)
    assert len(outcomes) == 1
    assert outcomes[0].ok is False
    assert outcomes[0].error == "TRUST_REQUIRED"


def test_engine_shell_requires_capability():
    decl = HookDeclaration(
        name="noexec", event="PostToolUse", handler_type="shell", handler="echo hi", source="user"
    )
    engine = HookEngine()
    engine.add(decl)
    outcomes = engine.emit("PostToolUse", {})
    assert outcomes[0].error.startswith("CAPABILITY_REQUIRED")


def test_engine_python_handler_captures_output(tmp_path):
    mod = tmp_path / "myhook.py"
    mod.write_text(
        "def run(payload):\n    return {'saw': payload.get('tool')}\n",
        encoding="utf-8",
    )
    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        engine = HookEngine(timeout_s=5)
        engine.add(
            HookDeclaration(
                name="py",
                event="PostToolUse",
                handler_type="python",
                handler="myhook:run",
                source="user",
            )
        )
        outcomes = engine.emit("PostToolUse", {"tool": "fs.read"})
        assert outcomes[0].ok is True
        assert json.loads(outcomes[0].output) == {"saw": "fs.read"}
    finally:
        sys.path.remove(str(tmp_path))


def test_engine_failure_does_not_stop_later_hooks(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def boom(payload):\n    raise ValueError('nope')\n", encoding="utf-8")
    good = tmp_path / "good.py"
    good.write_text("def ok(payload):\n    return 'fine'\n", encoding="utf-8")
    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        engine = HookEngine()
        engine.add(
            HookDeclaration(
                name="aa",
                event="PostToolUse",
                handler_type="python",
                handler="bad:boom",
                source="user",
            )
        )
        engine.add(
            HookDeclaration(
                name="bb",
                event="PostToolUse",
                handler_type="python",
                handler="good:ok",
                source="user",
            )
        )
        outcomes = engine.emit("PostToolUse", {})
        by = {o.name: o for o in outcomes}
        assert by["aa"].ok is False
        assert "ValueError" in by["aa"].error
        assert by["bb"].ok is True
    finally:
        sys.path.remove(str(tmp_path))


def test_engine_shell_handler_echo():
    decl = HookDeclaration(
        name="sh",
        event="PostToolUse",
        handler_type="shell",
        handler="echo hooked",
        source="user",
        capabilities=("shell.exec",),
    )
    engine = HookEngine(timeout_s=5)
    engine.add(decl)
    outcomes = engine.emit("PostToolUse", {})
    assert outcomes[0].ok is True
    assert "hooked" in outcomes[0].output.strip()


def test_engine_traces_outcome_callback():
    seen = []
    engine = HookEngine()
    engine.on_outcome = seen.append
    engine.add(
        HookDeclaration(
            name="x",
            event="PostToolUse",
            handler_type="python",
            handler="__main__:nope",
            source="user",
        )
    )
    engine.emit("PostToolUse", {})
    assert len(seen) == 1
    assert seen[0].name == "x"


def test_all_events_catalog_complete():
    assert len(ALL_EVENTS) == 13
    assert "SessionStart" in ALL_EVENTS
    assert "SubagentStop" in ALL_EVENTS


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@pytest.fixture
def services(app_ctx):
    return build_services(app_ctx)


def write_user_hooks(services, entries):
    path = services.hooks.user_hooks_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_service_discover_and_state(services):
    write_user_hooks(
        services,
        [
            {"name": "h1", "event": "PreToolUse", "handler_type": "python", "handler": "m:f"},
            {
                "name": "h2",
                "event": "PostToolUse",
                "handler_type": "shell",
                "command": "echo x",
                "capabilities": ["shell.exec"],
            },
        ],
    )
    rows = services.hooks.list()
    assert {r["name"] for r in rows} == {"h1", "h2"}
    # Disable h1 -> state flips.
    services.hooks.disable("h1", "user")
    rows = services.hooks.list()
    h1 = next(r for r in rows if r["name"] == "h1")
    assert h1["enabled"] is False
    services.hooks.enable("h1", "user")
    rows = services.hooks.list()
    assert next(r for r in rows if r["name"] == "h1")["enabled"] is True


def test_service_build_engine_skips_disabled(services):
    write_user_hooks(
        services,
        [
            {"name": "off", "event": "PreToolUse", "handler_type": "python", "handler": "m:f"},
            {"name": "on", "event": "PreToolUse", "handler_type": "python", "handler": "m:g"},
        ],
    )
    services.hooks.disable("off", "user")
    engine: HookEngine = services.hooks.build_engine()
    names = [d.name for d in engine.matching("PreToolUse")]
    assert names == ["on"]


def test_service_project_hooks_trust_gating(services, tmp_path):
    project = tmp_path / "proj"
    (project / ".rinari").mkdir(parents=True)
    (project / ".rinari" / "hooks.json").write_text(
        json.dumps(
            [{"name": "ph", "event": "PostToolUse", "handler_type": "python", "handler": "m:h"}]
        ),
        encoding="utf-8",
    )
    decls = services.hooks.discover(project)
    assert [d.name for d in decls if d.source == "project"] == ["ph"]
    # Untrusted: emit is skipped.
    outcomes = services.hooks.build_engine(project=project).emit("PostToolUse", {}, project=project)
    assert all(o.error == "TRUST_REQUIRED" for o in outcomes)
    # Trusted: runs.
    services.trust.add(project)
    outcomes = services.hooks.build_engine(project=project).emit("PostToolUse", {}, project=project)
    assert not outcomes or all(o.ok or o.error != "TRUST_REQUIRED" for o in outcomes)


def test_service_test_dry_run(services, tmp_path):
    write_user_hooks(
        services,
        [
            {
                "name": "t",
                "event": "PostToolUse",
                "handler_type": "shell",
                "command": "echo dry",
                "capabilities": ["shell.exec"],
            }
        ],
    )
    outcomes = services.hooks.test("PostToolUse", {})
    assert outcomes and outcomes[0]["ok"] is True


def test_service_doctor_absent_and_ok(services):
    report = services.hooks.doctor()
    assert any(e.get("status") == "absent" for e in report if e.get("file") is not None)
    write_user_hooks(
        services,
        [{"name": "ok", "event": "SessionEnd", "handler_type": "python", "handler": "m:f"}],
    )
    report = services.hooks.doctor()
    ok = next(e for e in report if e.get("file") and e["status"] == "ok")
    assert ok["hooks"] == 1


def test_service_doctor_reports_bad_declarations(services):
    write_user_hooks(
        services, [{"name": "bad", "event": "Bogus", "handler_type": "python", "handler": "m:f"}]
    )
    report = services.hooks.doctor()
    errors = [e for e in report if e.get("status") == "errors"]
    assert errors


def test_session_lifecycle_session_start_and_end(monkeypatch, app_ctx, tmp_path):
    from dataclasses import dataclass

    from rinari.application.provider_service import AddProviderInput
    from rinari.cli import agent_runtime
    from rinari.cli.agent_runtime import build_agent_session
    from rinari.models.types import ModelRequest, ModelResponse, ProviderCapabilities, StopReason

    user_home = tmp_path / "home"
    user_home.mkdir()
    s = build_services(app_ctx, user_home=user_home)
    s.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    s.models.add("fake", "fake-model-1", "fake-one")
    s.providers.use("fake")

    @dataclass
    class FakeModel:
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(streaming=False, tool_calls=False)

        def invoke(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(content="ok", stop_reason=StopReason.END_TURN)

    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: FakeModel())

    seen: list[str] = []
    real_engine_builder = agent_runtime._build_hook_engine

    def spy(services, root):
        engine = real_engine_builder(services, root)
        if engine is not None:
            original = engine.emit

            def emit(event, payload, project=None):
                seen.append(event)
                return original(event, payload, project=project)

            engine.emit = emit
        return engine

    monkeypatch.setattr(agent_runtime, "_build_hook_engine", spy)

    cwd = tmp_path / "work"
    cwd.mkdir()
    record = s.sessions.start(cwd, forced_chat=True).session
    session = build_agent_session(s, record, interactive=False, user_home=user_home)
    assert "SessionStart" in seen
    session.end()
    assert "SessionEnd" in seen
    # end() is idempotent: a second call must not re-emit.
    session.end()
    assert seen.count("SessionEnd") == 1


# ---------------------------------------------------------------------------
# Agent-loop wiring
# ---------------------------------------------------------------------------


class ScriptedProvider:
    def __init__(self, tool_name: str, answer: str):
        self._tool_name = tool_name
        self._answer = answer
        self._calls = 0

    def capabilities(self):
        class Caps:
            streaming = False
            max_context_tokens = 100_000

        return Caps()

    def invoke(self, request, on_delta=None):

        self._calls += 1
        if self._calls == 1:
            return _tc_response(self._tool_name)
        return _answer_response(self._answer)


def _tc_response(tool_name: str):
    from rinari.models.types import ModelResponse, StopReason, ToolCall, Usage

    return ModelResponse(
        content="",
        stop_reason=StopReason.TOOL_CALLS,
        tool_calls=(ToolCall(id="tc1", name=tool_name, arguments={"path": "x"}),),
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def _answer_response(answer: str):
    from rinari.models.types import ModelResponse, StopReason, Usage

    return ModelResponse(
        content=answer,
        stop_reason=StopReason.END_TURN,
        tool_calls=(),
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def test_agent_loop_emits_hook_lifecycle(tmp_path):
    from rinari.policy.approvals import ApprovalEngine
    from rinari.policy.engine import PermissionProfile, PolicyEngine
    from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
    from rinari.prompts.assembler import AssemblerContext, PromptAssembler
    from rinari.runtime.agent import AgentContext, AgentLoop
    from rinari.runtime.cancellation import CancellationToken
    from rinari.shared.clock import FakeClock
    from rinari.tools.definition import (
        ClassifiedAction,
        ToolContext,
        ToolDefinition,
        ToolErrorCode,
        ToolErrorInfo,
        ToolResult,
    )
    from rinari.tools.registry import ToolRegistry
    from rinari.tools.runtime import ToolRuntime

    events: list[tuple[str, dict]] = []

    def sink(event, payload):
        events.append((event, payload))

    root = tmp_path / "work"
    root.mkdir(parents=True, exist_ok=True)
    failing = "shell.exec"

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="shell.exec",
            description="run",
            input_schema={"type": "object", "properties": {"command": {"type": "string"}}},
            capabilities=("shell.exec",),
            classify=lambda _i: ClassifiedAction("shell.exec", "ls"),
            handler=lambda args, ctx: ToolResult(
                ok=False, error=ToolErrorInfo(ToolErrorCode.UNKNOWN, "denied")
            ),
        )
    )
    runtime = ToolRuntime(
        registry,
        PolicyEngine(),
        ApprovalEngine(prompt=lambda r: "n"),
        clock=FakeClock(),
    )
    tool_ctx = ToolContext(
        session_id="s-hooks",
        kind="CHAT",
        cwd=root,
        project_root=None,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=30, max_output_bytes=65536),
        artifact_root=tmp_path / "art",
        clock=FakeClock(),
        cancellation=CancellationToken(),
    )
    loop = AgentLoop(
        ScriptedProvider(failing, "done"),
        runtime,
        PromptAssembler(),
        hook_sink=sink,
    )
    ctx = AgentContext(
        session_id="s-hooks",
        model_ref="/fake",
        tool_ctx=tool_ctx,
        assembler_base=AssemblerContext(
            session_kind="CHAT",
            constitution="",
            runtime_policy="",
            soul="",
        ),
    )
    result = loop.turn(ctx, "do it")
    assert result.kind == "answer"
    names = [n for n, _ in events]
    assert names == [
        "BeforeModel",
        "AfterModel",
        "PreToolUse",
        "PostToolUse",
        "ToolError",
        "BeforeModel",
        "AfterModel",
        "BeforeFinal",
    ]
