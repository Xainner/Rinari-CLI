"""E2E hermetic harness flows (Etapa E / review §11.2).

No network, no real providers: a scripted FakeModel drives the real
AgentLoop + ToolRuntime + native tools + budgets + exposure + approvals,
so the failures that only appear when chaining (model → tool →
persistence → approval → error → retry → resume) are covered.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from rinari.capability_search import capability_activation_tools, capability_search_tool
from rinari.models.types import ModelRequest, ModelResponse, StopReason, ToolCall
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.prompts.assembler import AssemblerContext, PromptAssembler
from rinari.runtime.agent import AgentContext, AgentLoop
from rinari.runtime.budget import BudgetMeter, TurnBudgetLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.runtime.model_caller import SessionModelGateway
from rinari.shared.clock import FakeClock
from rinari.shared.errors import CancelledError
from rinari.shared.redaction import Redactor
from rinari.tools.definition import ClassifiedAction, ToolContext, ToolDefinition
from rinari.tools.exposure import ToolExposure
from rinari.tools.native import all_native_tools
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime


@dataclass
class FakeModel:
    scripted: list[ModelResponse]
    streaming: bool = False
    requests: list[ModelRequest] = field(default_factory=list)

    def capabilities(self):
        from rinari.models.types import ProviderCapabilities

        return ProviderCapabilities(streaming=self.streaming, tool_calls=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.scripted.pop(0)

    def invoke_stream(self, request, on_delta) -> ModelResponse:
        self.requests.append(request)
        response = self.scripted.pop(0)
        if response.content:
            on_delta(response.content)
        return response


@dataclass
class StubCaller:
    """Duck-typed ModelCaller around a FakeModel (gateway delegation)."""

    model: FakeModel

    def capabilities(self):
        return self.model.capabilities()

    def invoke(self, request):
        return self.model.invoke(request)

    def invoke_stream(self, request, on_delta):
        return self.model.invoke_stream(request, on_delta)


def _harness(tmp_path: Path, *, approvals: str = "n", extra_tools=()):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("hello\n", encoding="utf-8")
    clock = FakeClock(start=1_700_000_000.0, step=0.05)
    registry = ToolRegistry()
    registry.register_all(all_native_tools())
    registry.register_all(list(extra_tools))
    runtime = ToolRuntime(
        registry,
        PolicyEngine(),
        ApprovalEngine(prompt=lambda req: approvals),
        clock=clock,
        redactor=Redactor(),
    )
    tool_ctx = ToolContext(
        session_id="e2e",
        kind="PROJECT",
        cwd=root,
        project_root=root,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=30, max_output_bytes=65536),
        artifact_root=tmp_path / "artifacts",
        clock=clock,
        cancellation=CancellationToken(),
    )
    ctx = AgentContext(
        session_id="e2e",
        model_ref="fake",
        tool_ctx=tool_ctx,
        assembler_base=AssemblerContext(
            session_kind="PROJECT",
            constitution="Test constitution.",
            soul="Test soul.",
            environment={"cwd": str(root)},
        ),
    )
    return {"root": root, "clock": clock, "registry": registry, "runtime": runtime, "ctx": ctx}


def _answer(text: str) -> ModelResponse:
    return ModelResponse(content=text)


def _calls(*calls: ToolCall) -> ModelResponse:
    return ModelResponse(content="", tool_calls=tuple(calls), stop_reason=StopReason.TOOL_CALLS)


# -- long coding turn ------------------------------------------------------------


def test_e2e_long_coding_turn(tmp_path) -> None:
    env = _harness(tmp_path)
    model = FakeModel(
        scripted=[
            _calls(ToolCall(id="t1", name="fs.list", arguments={"path": "src"})),
            _calls(ToolCall(id="t2", name="fs.read", arguments={"path": "src/a.py"})),
            _calls(
                ToolCall(
                    id="t3",
                    name="fs.write",
                    arguments={"path": "src/b.py", "content": "world\n"},
                )
            ),
            _calls(ToolCall(id="t4", name="fs.read", arguments={"path": "src/b.py"})),
            _answer("done"),
        ]
    )
    loop = AgentLoop(model, env["runtime"], PromptAssembler())
    result = loop.turn(
        env["ctx"], "Scaffold b.py", budget=BudgetMeter(TurnBudgetLimits(), env["clock"])
    )
    assert result.kind == "answer"
    assert result.tool_calls == 4
    assert (env["root"] / "src" / "b.py").read_text() == "world\n"


# -- exact budget boundary ----------------------------------------------------------


def test_e2e_tool_budget_boundary_exact(tmp_path) -> None:
    env = _harness(tmp_path)
    for name in ("b.py", "c.py", "d.py"):
        (env["root"] / "src" / name).write_text("x\n", encoding="utf-8")
    model = FakeModel(
        scripted=[
            _calls(
                *(
                    ToolCall(id=f"t{i}", name="fs.read", arguments={"path": f"src/{n}"})
                    for i, n in enumerate(("a.py", "b.py", "c.py", "d.py"))
                )
            ),
            _answer("stopped"),
        ]
    )
    loop = AgentLoop(model, env["runtime"], PromptAssembler())
    meter = BudgetMeter(TurnBudgetLimits(max_tool_calls=3), env["clock"])
    result = loop.turn(env["ctx"], "Read a lot", budget=meter)
    assert result.tool_calls == 3
    assert meter.tool_calls == 3
    envelope = env["ctx"].history[-2].content or ""
    assert "RESOURCE_EXHAUSTED" in envelope


# -- dynamic tools: 150 registered, budgeted exposure, search→activate -----------------


def _lazy_plugin_tool(i: int) -> ToolDefinition:
    return ToolDefinition(
        name=f"plugin.tool_{i}",
        description=f"plugin capability number {i} for widget processing",
        input_schema={"type": "object", "properties": {}},
        manifest={"source": "plugin"},
        classify=lambda args: ClassifiedAction(f"plugin.tool_{i}"),
        handler=lambda args, ctx: {"ok": True},
    )


def test_e2e_dynamic_tools_budget_and_activation(tmp_path) -> None:
    env = _harness(tmp_path, approvals="y")
    registry = env["registry"]
    registry.register_all([_lazy_plugin_tool(i) for i in range(150)])
    registry.register(capability_search_tool(registry))
    registry.register_all(capability_activation_tools(registry))
    assert len(registry.names()) >= 150

    env["ctx"].tool_ctx = replace(env["ctx"].tool_ctx, exposure=ToolExposure())

    model = FakeModel(
        scripted=[
            _calls(ToolCall(id="s1", name="capability.search", arguments={"query": "tool_7"})),
            _calls(
                ToolCall(
                    id="a1",
                    name="capability.activate",
                    arguments={"names": ["plugin.tool_7"], "scope": "session"},
                )
            ),
            _answer("activated"),
        ]
    )
    loop = AgentLoop(model, env["runtime"], PromptAssembler())
    result = loop.turn(
        env["ctx"], "Find widget tooling", budget=BudgetMeter(TurnBudgetLimits(), env["clock"])
    )
    assert result.kind == "answer"
    search_msg = env["ctx"].history[2]
    assert "plugin.tool_7" in (search_msg.content or "")
    first = {t.name for t in model.requests[0].tools}
    assert "plugin.tool_7" not in first
    assert len(model.requests[0].tools) <= 96
    last = {t.name for t in model.requests[-1].tools}
    assert "plugin.tool_7" in last


# -- provider switch mid-session ---------------------------------------------------------


def test_e2e_provider_switch_next_request_goes_to_b(tmp_path) -> None:
    env = _harness(tmp_path)
    model_a = FakeModel(scripted=[_answer("from A")])
    model_b = FakeModel(scripted=[_answer("from B")])
    gateway = SessionModelGateway(StubCaller(model_a))
    loop = AgentLoop(gateway, env["runtime"], PromptAssembler())
    first = loop.turn(env["ctx"], "hi")
    assert first.content == "from A"
    gateway.switch(StubCaller(model_b))
    second = loop.turn(env["ctx"], "hi again")
    assert second.content == "from B"
    assert len(model_a.requests) == 1 and len(model_b.requests) == 1
    roles = [m.role for m in env["ctx"].history]
    assert roles == ["user", "assistant", "user", "assistant"]


# -- large output: spill + bounded observation + artifact.read ------------------------------


def test_e2e_large_output_spills_and_stays_bounded(tmp_path) -> None:
    big = "z" * (1024 * 1024)
    tool = ToolDefinition(
        name="test.big",
        description="big output",
        input_schema={"type": "object", "properties": {}},
        classify=lambda args: ClassifiedAction("fs.read", str(args.get("path") or "")),
        handler=lambda args, ctx: big,
    )
    env = _harness(tmp_path, approvals="y", extra_tools=[tool])
    result = env["runtime"].execute(
        "test.big",
        {"path": str(env["root"] / "src" / "a.py")},
        env["ctx"].tool_ctx,
        tool_call_id="big1",
    )
    assert result.ok and result.truncated
    assert len(result.artifacts) == 1
    uri = result.artifacts[0].uri
    text = result.to_model_text("test.big")
    assert len(text) < 100_000
    back = env["runtime"].execute(
        "artifact.read",
        {"uri": uri, "start_byte": 0, "max_bytes": 16},
        env["ctx"].tool_ctx,
        tool_call_id="big2",
    )
    assert back.ok


# -- approval deny: no execution, no double exec ----------------------------------------------


def test_e2e_approval_deny_never_executes(tmp_path) -> None:
    attempts = {"n": 0}

    def handler(args, ctx):
        attempts["n"] += 1
        return {"ok": True}

    tool = ToolDefinition(
        name="test.asky",
        description="needs approval",
        input_schema={"type": "object", "properties": {}},
        classify=lambda args: ClassifiedAction("test.asky"),
        handler=handler,
    )
    env = _harness(tmp_path, approvals="n", extra_tools=[tool])
    model = FakeModel(
        scripted=[
            _calls(
                ToolCall(id="t1", name="test.asky", arguments={}),
                ToolCall(id="t2", name="test.asky", arguments={}),
            ),
            _answer("denied"),
        ]
    )
    loop = AgentLoop(model, env["runtime"], PromptAssembler())
    result = loop.turn(env["ctx"], "Try it", budget=BudgetMeter(TurnBudgetLimits(), env["clock"]))
    assert result.kind == "answer"
    assert attempts["n"] == 0
    denied = [m for m in env["ctx"].history if m.role == "tool"]
    assert len(denied) == 2
    assert all("APPROVAL_DENIED" in (m.content or "") for m in denied)


# -- cancellation: stream abort keeps the session usable ----------------------------------------


def test_e2e_cancel_stream_then_recover(tmp_path) -> None:
    env = _harness(tmp_path)
    token = env["ctx"].tool_ctx.cancellation
    token.cancel()
    model = FakeModel(scripted=[_answer("never")], streaming=True)
    loop = AgentLoop(model, env["runtime"], PromptAssembler())
    with pytest.raises(CancelledError):
        loop.turn(
            env["ctx"],
            "start",
            on_delta=lambda d: None,
            budget=BudgetMeter(TurnBudgetLimits(), env["clock"]),
        )
    token.reset()
    model2 = FakeModel(scripted=[_answer("recovered")])
    loop2 = AgentLoop(model2, env["runtime"], PromptAssembler())
    result = loop2.turn(env["ctx"], "again", budget=BudgetMeter(TurnBudgetLimits(), env["clock"]))
    assert result.content == "recovered"


# -- MCP structured variants ----------------------------------------------------------------------


def test_e2e_mcp_structured_content_variants() -> None:
    from rinari.mcp.service import McpService

    for value in ({"v": 1}, [1, 2], 42, "text", True, None):

        class FakeClient:
            def call_tool(self, tool, arguments, _value=value):
                return {"content": [], "structuredContent": _value}

        class FakeService(McpService):
            def _client(self, name, project=None):
                return FakeClient()

        service = FakeService.__new__(FakeService)
        payload = McpService.invoke(service, "srv", "t", {})
        if value is None:
            assert "structuredContent" not in payload
        else:
            assert payload["structuredContent"] == value
