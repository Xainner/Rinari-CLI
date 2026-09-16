"""Browser-use loop proof (phase 2 exit): observe -> bind -> act.

Runs the real AgentLoop + ToolRuntime + approvals against the loopback fake
CDP server. No real browser, no network, no paid model calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rinari.browser.manager import BrowserManager
from rinari.models.types import ModelRequest, ModelResponse, StopReason, ToolCall
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.policy.network import NetworkGuard, NetworkPolicy
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.prompts.assembler import AssemblerContext, PromptAssembler
from rinari.runtime.agent import AgentContext, AgentLoop
from rinari.runtime.budget import BudgetMeter, TurnBudgetLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.shared.redaction import Redactor
from rinari.tools.definition import ToolContext
from rinari.tools.native import all_native_tools
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime
from tests.unit.test_browser_cdp import FakeCdpServer


@dataclass
class FakeModel:
    scripted: list[ModelResponse]

    def capabilities(self):
        from rinari.models.types import ProviderCapabilities

        return ProviderCapabilities(tool_calls=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        return self.scripted.pop(0)

    def invoke_stream(self, request, on_delta) -> ModelResponse:
        return self.invoke(request)


def _answer(text: str) -> ModelResponse:
    return ModelResponse(content=text)


def _calls(*calls: ToolCall) -> ModelResponse:
    return ModelResponse(content="", tool_calls=tuple(calls), stop_reason=StopReason.TOOL_CALLS)


def _harness(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("RINARI_BROWSER_CDP", raising=False)
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("hello" + chr(10), encoding="utf-8")
    server = FakeCdpServer().start()
    manager = BrowserManager(session_id="e2e-use", home_root=tmp_path / "home")
    manager.connect(server.base_url)
    clock = FakeClock(start=1_700_000_000.0, step=0.05)
    registry = ToolRegistry()
    registry.register_all(all_native_tools())
    runtime = ToolRuntime(
        registry,
        PolicyEngine(network=NetworkPolicy(mode="allow")),
        ApprovalEngine(prompt=lambda req: "s"),
        clock=clock,
        redactor=Redactor(),
    )
    tool_ctx = ToolContext(
        session_id="e2e-use",
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
        network=NetworkGuard(NetworkPolicy(mode="allow")),
        browser=manager,
    )
    ctx = AgentContext(
        session_id="e2e-use",
        model_ref="fake",
        tool_ctx=tool_ctx,
        assembler_base=AssemblerContext(
            session_kind="PROJECT",
            constitution="Test constitution.",
            soul="Test soul.",
            environment={"cwd": str(root)},
        ),
    )
    return {"server": server, "manager": manager, "clock": clock, "runtime": runtime, "ctx": ctx}


def _turn(env, model, message: str):
    loop = AgentLoop(model, env["runtime"], PromptAssembler())
    return loop.turn(env["ctx"], message, budget=BudgetMeter(TurnBudgetLimits(), env["clock"]))


def test_observe_then_bound_click(tmp_path, monkeypatch) -> None:
    env = _harness(tmp_path, monkeypatch)
    try:
        first = _turn(
            env,
            FakeModel(
                scripted=[
                    _calls(
                        ToolCall(id="s1", name="browser.snapshot", arguments={"target_id": "t1"})
                    ),
                    _answer("page seen"),
                ]
            ),
            "Look at the page",
        )
        assert first.kind == "answer"
        assert first.tool_calls == 1
        record = env["manager"]._observations.get("t1")
        assert record and record["observation_id"]
        second = _turn(
            env,
            FakeModel(
                scripted=[
                    _calls(
                        ToolCall(
                            id="c1",
                            name="browser.click",
                            arguments={
                                "selector": "#a",
                                "target_id": "t1",
                                "observation_id": record["observation_id"],
                            },
                        )
                    ),
                    _answer("link clicked"),
                ]
            ),
            "Click the link you observed",
        )
        assert second.kind == "answer"
        assert second.tool_calls == 1
        pressed = [
            params
            for method, params, _session in env["server"].commands
            if method == "Input.dispatchMouseEvent" and params.get("type") == "mousePressed"
        ]
        assert pressed, "bound click must reach the page input pipeline"
    finally:
        env["manager"].close()
        env["server"].stop()


def test_stale_observation_surfaces_to_model_without_retry(tmp_path, monkeypatch) -> None:
    env = _harness(tmp_path, monkeypatch)
    try:
        seen = _turn(
            env,
            FakeModel(
                scripted=[
                    _calls(
                        ToolCall(id="s1", name="browser.snapshot", arguments={"target_id": "t1"})
                    ),
                    _answer("page seen"),
                ]
            ),
            "Look at the page",
        )
        assert seen.kind == "answer"
        env["manager"].navigate("t1", "https://example.com/other")
        fallen = _turn(
            env,
            FakeModel(
                scripted=[
                    _calls(
                        ToolCall(
                            id="c1",
                            name="browser.click",
                            arguments={
                                "selector": "#a",
                                "target_id": "t1",
                                "observation_id": "deadbeefcafe",
                            },
                        )
                    ),
                    _answer("blocked: will re-observe"),
                ]
            ),
            "Click with the old observation",
        )
        # The stale input fails once, visibly, and the loop does not spin:
        # one tool call, then the scripted answer ends the turn.
        assert fallen.kind == "answer"
        assert fallen.tool_calls == 1
    finally:
        env["manager"].close()
        env["server"].stop()
