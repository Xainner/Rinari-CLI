"""Turn budgets + loop detection tests (phase 4).

Deterministic: scripted model responses, real tools/policy, FakeClock, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from rinari.models.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
    ToolCall,
    Usage,
)
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.prompts.assembler import AssemblerContext, PromptAssembler
from rinari.runtime.agent import AgentContext, AgentLoop
from rinari.runtime.budget import BudgetMeter, TurnBudgetLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.runtime.loopdetection import (
    NUDGE,
    STOP,
    LoopDetector,
)
from rinari.shared.clock import FakeClock
from rinari.shared.redaction import Redactor
from rinari.tools.definition import ToolContext
from rinari.tools.native import all_native_tools
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime


@dataclass
class FakeModel:
    scripted: list[ModelResponse]
    requests: list[ModelRequest] = field(default_factory=list)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.scripted.pop(0)

    def invoke_stream(self, request: ModelRequest, on_delta) -> ModelResponse:
        raise AssertionError("streaming not expected in these tests")


def make_tool_runtime(tmp_path: Path, root: Path, clock):
    registry = ToolRegistry()
    registry.register_all(all_native_tools())
    runtime = ToolRuntime(
        registry,
        PolicyEngine(),
        ApprovalEngine(prompt=lambda req: "n"),
        clock=clock,
        redactor=Redactor(),
    )
    return runtime


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "a.txt").write_text("hello\n")
    clock = FakeClock(start=1_700_000_000.0, step=0.0)
    runtime = make_tool_runtime(tmp_path, root, clock)
    assembler = PromptAssembler()
    assembler_base = AssemblerContext(
        session_kind="PROJECT",
        constitution="You are RINARI, following the project constitution.",
        soul="Rinari identity: precise, safe, transparent.",
        environment={"cwd": str(root)},
    )
    tool_ctx = ToolContext(
        session_id="s1",
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
        session_id="s1",
        model_ref="fake-model",
        tool_ctx=tool_ctx,
        assembler_base=assembler_base,
    )
    return {"root": root, "clock": clock, "runtime": runtime, "assembler": assembler, "ctx": ctx}


def _read_call(n: int) -> ToolCall:
    return ToolCall(id=f"tc{n}", name="fs.read", arguments={"path": "a.txt"})


# ---------------------------------------------------------------------------
# BudgetMeter unit behavior
# ---------------------------------------------------------------------------


def test_model_calls_exhaustion() -> None:
    meter = BudgetMeter(TurnBudgetLimits(max_model_calls=2), FakeClock())
    meter.note_model_call()
    assert meter.exhausted() == ()
    meter.note_model_call()
    # Gate (>=) and exhausted() agree: at the ceiling the dimension reports.
    assert meter.first_exhausted() == "model-calls"


def test_tool_gate_and_exhaustion() -> None:
    meter = BudgetMeter(TurnBudgetLimits(max_tool_calls=2), FakeClock())
    assert meter.allows_tool("fs.read")
    meter.note_tool_call("fs.read")
    assert meter.allows_tool("fs.read")
    meter.note_tool_call("fs.read")
    assert meter.allows_tool("fs.read") is False
    assert meter.exhausted() == ("tool-calls",)


def test_tool_budget_boundary_with_meter(env) -> None:
    """With a meter present it is authoritative: N execute, N+1 rejected."""
    from rinari.runtime.agent import AgentLoop

    calls = tuple(
        ToolCall(id=f"tc{i}", name="fs.stat", arguments={"path": f"f{i}.txt"}) for i in range(5)
    )
    model = FakeModel(
        scripted=[
            ModelResponse(content="", tool_calls=calls, stop_reason=StopReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    loop = AgentLoop(model, env["runtime"], env["assembler"])
    budget = BudgetMeter(TurnBudgetLimits(max_tool_calls=3), env["clock"])
    result = loop.turn(env["ctx"], "go", budget=budget)
    assert result.kind == "answer"
    assert result.tool_calls == 3
    assert budget.tool_calls == 3
    tools_msgs = [m for m in env["ctx"].history if m.role == "tool"]
    assert len(tools_msgs) == 5
    # Rejected calls become budget-exhausted observations, never executions.
    # (P0.4 will also carry the machine-readable RESOURCE_EXHAUSTED code.)
    assert "per-turn tool budget exhausted" in tools_msgs[3].content
    assert "per-turn tool budget exhausted" in tools_msgs[4].content
    assert "per-turn tool budget exhausted" not in tools_msgs[2].content


def test_default_emergency_budget_does_not_stop_useful_sixty_five_calls(env) -> None:
    """Normal work is not stopped by the former 64-call task budget."""
    from rinari.runtime.agent import AgentLoop

    calls = tuple(
        ToolCall(id=f"tc{i}", name="fs.stat", arguments={"path": f"g{i}.txt"}) for i in range(65)
    )
    model = FakeModel(
        scripted=[
            ModelResponse(content="", tool_calls=calls, stop_reason=StopReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    loop = AgentLoop(model, env["runtime"], env["assembler"])
    budget = BudgetMeter(TurnBudgetLimits(), FakeClock())
    result = loop.turn(env["ctx"], "go", budget=budget)
    assert result.kind == "answer"
    assert result.tool_calls == 65
    assert budget.tool_calls == 65
    tools_msgs = [m for m in env["ctx"].history if m.role == "tool"]
    assert len(tools_msgs) == 65
    assert "per-turn tool budget exhausted" not in tools_msgs[64].content
    assert "per-turn tool budget exhausted" not in tools_msgs[63].content


def test_fallback_does_not_reintroduce_small_tool_budget(env) -> None:
    """Without a meter the defensive fallback is still an emergency limit."""
    from rinari.runtime.agent import AgentLoop

    calls = tuple(
        ToolCall(id=f"tc{i}", name="fs.stat", arguments={"path": f"h{i}.txt"}) for i in range(33)
    )
    model = FakeModel(
        scripted=[
            ModelResponse(content="", tool_calls=calls, stop_reason=StopReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    loop = AgentLoop(model, env["runtime"], env["assembler"])
    result = loop.turn(env["ctx"], "go", budget=None)
    assert result.kind == "answer"
    assert result.tool_calls == 33


def test_network_dimension_counts_only_network_namespaces() -> None:
    meter = BudgetMeter(TurnBudgetLimits(max_network_calls=1), FakeClock())
    meter.note_tool_call("fs.read")
    meter.note_tool_call("web.fetch")
    assert meter.network_calls == 1
    assert meter.allows_tool("web.fetch") is False
    assert meter.allows_tool("fs.read") is True
    meter.note_tool_call("web.fetch")
    assert "network-calls" in meter.exhausted()


def test_wall_time_uses_injected_clock() -> None:
    clock = FakeClock(start=100.0)
    meter = BudgetMeter(TurnBudgetLimits(max_wall_time_s=600.0), clock)
    assert meter.elapsed_s() == 0.0
    clock.advance(601.0)
    assert meter.first_exhausted() == "wall-time"


def test_cost_never_invented_without_pricing() -> None:
    meter = BudgetMeter(TurnBudgetLimits(max_cost=0.01), FakeClock())
    meter.note_usage(Usage(input_tokens=2_000_000, output_tokens=100_000))
    assert meter.estimated_cost() is None
    assert "cost" not in meter.exhausted()


def test_cost_exhausted_with_pricing() -> None:
    limits = TurnBudgetLimits(
        max_cost=1.0,
        input_price_per_mtok=2.0,
        output_price_per_mtok=4.0,
    )
    meter = BudgetMeter(limits, FakeClock())
    meter.note_usage(Usage(input_tokens=500_000, output_tokens=100_000))
    assert meter.estimated_cost() == pytest.approx(1.4)
    assert "cost" in meter.exhausted()
    assert meter.first_exhausted() == "cost"  # wall-time unset; cost only hit


def test_subagent_and_recursion_dimensions() -> None:
    meter = BudgetMeter(
        TurnBudgetLimits(max_subagent_calls=2, max_recursion_depth=2),
        FakeClock(),
    )
    meter.note_subagent(depth=3)
    assert meter.exhausted() == ("recursion-depth",)
    meter.note_subagent(depth=1)
    assert meter.first_exhausted() == "subagents"


def test_spawn_child_ledger() -> None:
    parent = BudgetMeter(TurnBudgetLimits(max_tool_calls=64), FakeClock())
    child = parent.spawn_child(TurnBudgetLimits(max_tool_calls=4))
    assert parent.subagent_calls == 1
    assert child.limits.max_tool_calls == 4
    child.note_model_call()
    child.note_tool_call("fs.read")
    child.note_usage(Usage(input_tokens=10, output_tokens=5))
    # The parent ledger reflects the child's real cost.
    assert parent.model_calls == 1
    assert parent.tool_calls == 1
    assert parent.input_tokens == 10
    assert parent.output_tokens == 5
    # The child's own gates still apply to the child alone.
    assert child.allows_tool("fs.read")
    for _ in range(3):
        child.note_tool_call("fs.read")
    assert child.allows_tool("fs.read") is False
    assert parent.tool_calls == 4


def test_spawn_child_depth_relativized() -> None:
    parent = BudgetMeter(TurnBudgetLimits(), FakeClock())
    child = parent.spawn_child(depth=2)
    assert parent.max_recursion_depth == 2
    child.spawn_child(depth=2)
    assert child.max_recursion_depth == 2
    assert parent.max_recursion_depth == 3


def test_snapshot_shape_and_priority_order() -> None:
    clock = FakeClock(start=0.0)
    meter = BudgetMeter(
        TurnBudgetLimits(max_model_calls=1, max_tool_calls=1, max_wall_time_s=1.0),
        clock,
    )
    meter.note_model_call()
    meter.note_model_call()
    meter.note_tool_call("fs.read")
    meter.note_tool_call("fs.read")
    clock.advance(10.0)
    snap = meter.snapshot()
    assert snap["model_calls"] == 2
    assert snap["tool_calls"] == 2
    assert snap["estimated_cost"] is None
    assert snap["exhausted"] == ["model-calls", "tool-calls", "wall-time"]


# ---------------------------------------------------------------------------
# LoopDetector unit behavior
# ---------------------------------------------------------------------------


def test_same_tool_args_nudge_then_stop() -> None:
    det = LoopDetector(repeats=3)
    for _ in range(2):
        det.record_tool("fs.read", {"path": "a.txt"})
        assert det.check() is None
    det.record_tool("fs.read", {"path": "a.txt"})
    signal = det.check()
    assert signal is not None
    assert signal.kind == "same-tool-args"
    assert signal.action == NUDGE
    assert "[harness loop-detector]" in det.nudge_text(signal)
    det.record_tool("fs.read", {"path": "a.txt"})
    assert det.check().action == STOP


def test_two_action_oscillation() -> None:
    det = LoopDetector()
    det.record_tool("fs.read", {"path": "a.txt"})
    det.record_tool("fs.write", {"path": "b.txt", "content": "1"})
    det.record_tool("fs.read", {"path": "a.txt"})
    signal = det.check()
    assert signal is None  # only three actions so far
    det.record_tool("fs.write", {"path": "b.txt", "content": "1"})
    signal = det.check()
    assert signal is not None
    assert signal.kind == "two-action-oscillation"
    assert signal.action == NUDGE


def test_repeated_rewrites_same_path() -> None:
    det = LoopDetector(repeats=3)
    for i in range(3):
        det.record_tool("fs.write", {"path": "x.txt", "content": f"v{i}"})
    signal = det.check()
    assert signal is not None
    assert signal.kind == "repeated-rewrites"
    assert signal.action == NUDGE


def test_repeated_denied_approval() -> None:
    det = LoopDetector(repeats=3)
    for _ in range(3):
        det.record_error("fs.write", "APPROVAL_DENIED", "denied by user")
    signal = det.check()
    assert signal is not None
    assert signal.kind == "repeated-denied-approval"
    assert signal.action == NUDGE


def test_same_error_signature() -> None:
    det = LoopDetector(repeats=3)
    for _ in range(3):
        det.record_error("fs.read", "NOT_FOUND", "No such file: missing.txt")
    signal = det.check()
    assert signal is not None
    assert signal.kind == "same-error"
    assert signal.action == NUDGE


def test_duplicated_subagent_work() -> None:
    det = LoopDetector(repeats=3)
    for _ in range(3):
        det.record_subagent("find the bug in auth")
    signal = det.check()
    assert signal is not None
    assert signal.kind == "duplicated-subagent-work"
    assert signal.action == NUDGE


def test_no_false_positive_on_varied_actions() -> None:
    det = LoopDetector()
    det.record_tool("fs.read", {"path": "a.txt"})
    det.record_tool("fs.read", {"path": "b.txt"})
    det.record_tool("fs.write", {"path": "a.txt", "content": "1"})
    assert det.check() is None


# ---------------------------------------------------------------------------
# AgentLoop integration: budgets and loop detection inside a real turn
# ---------------------------------------------------------------------------


def test_loop_nudge_then_stop_in_real_turn(env) -> None:
    model = FakeModel(
        scripted=[
            ModelResponse(
                content="", tool_calls=(_read_call(i),), stop_reason=StopReason.TOOL_CALLS
            )
            for i in range(4)
        ]
    )
    events: list[tuple[str, str, dict]] = []
    loop = AgentLoop(
        model,
        env["runtime"],
        env["assembler"],
        event_sink=lambda sid, t, p: events.append((sid, t, p)),
    )
    meter = BudgetMeter(TurnBudgetLimits(), clock=env["clock"])
    result = loop.turn(env["ctx"], "Read a.txt", budget=meter, loop=LoopDetector())
    assert result.kind == "loop"
    assert result.tool_calls == 4  # check runs after each execution
    assert len(model.requests) == 4
    # the nudge reached the model conversation before the 4th call
    joined = " ".join(m.content for m in model.requests[3].messages)
    assert "[harness loop-detector]" in joined
    loop_events = [p for _, t, p in events if t == "LoopDetected"]
    assert [p["action"] for p in loop_events] == ["nudge", "stop"]
    assert loop_events[0]["kind"] == "same-tool-args"
    # budget snapshot is observable on the turn result
    assert result.budget is not None
    assert result.budget["model_calls"] == 4
    completed = [p for _, t, p in events if t == "AgentTurnCompleted"]
    assert completed[-1]["kind"] == "loop"
    assert "budget" in completed[-1]


def test_budget_stops_turn_on_model_calls(env) -> None:
    model = FakeModel(
        scripted=[
            ModelResponse(
                content="", tool_calls=(_read_call(i),), stop_reason=StopReason.TOOL_CALLS
            )
            for i in range(3)
        ]
    )
    events: list[tuple[str, str, dict]] = []
    loop = AgentLoop(
        model,
        env["runtime"],
        env["assembler"],
        event_sink=lambda sid, t, p: events.append((sid, t, p)),
    )
    meter = BudgetMeter(TurnBudgetLimits(max_model_calls=3), clock=env["clock"])
    result = loop.turn(
        env["ctx"],
        "Read a.txt",
        budget=meter,
        loop=LoopDetector(repeats=1_000_000),  # defeat loop detection on purpose
    )
    assert result.kind == "budget"
    assert "model-calls" in result.content
    assert len(model.requests) == 3
    assert result.tool_calls == 3
    assert result.budget is not None
    assert result.budget["model_calls"] == 3


def test_answer_turn_reports_budget_snapshot(env) -> None:
    model = FakeModel(scripted=[ModelResponse(content="done")])
    loop = AgentLoop(model, env["runtime"], env["assembler"])
    meter = BudgetMeter(TurnBudgetLimits(), clock=env["clock"])
    result = loop.turn(env["ctx"], "hi", budget=meter)
    assert result.kind == "answer"
    assert result.budget is not None
    assert result.budget["model_calls"] == 1
    assert result.budget["exhausted"] == []
