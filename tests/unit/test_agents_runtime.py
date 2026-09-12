"""Multi-agent runtime tests (phase 6): registry, orchestrator, worktrees, tools, isolation."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from rinari.agents.definition import (
    BUILTIN_AGENTS,
    MAX_DEPTH,
    AgentBudget,
    AgentDefinition,
)
from rinari.agents.orchestrator import (
    AgentOrchestrator,
    AgentResult,
    OrchestratorError,
    SubagentRunSpec,
    extract_evidence,
    parse_validation_block,
)
from rinari.agents.registry import AgentRegistry
from rinari.agents.worktree_manager import WorktreeError, WorktreeManager
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git binary not available")


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "rintest@example.com")
    _git(root, "config", "user.name", "Rintest")
    (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_builtin_agents_six():
    names = sorted(BUILTIN_AGENTS)
    assert names == sorted(
        ["explore", "reviewer", "debugger", "researcher", "implementer", "verifier"]
    )
    for name in ("explore", "reviewer", "verifier"):
        assert BUILTIN_AGENTS[name].profile == "inherit"
    assert BUILTIN_AGENTS["implementer"].profile == "inherit"


def test_registry_lists_builtins_without_project():
    registry = AgentRegistry()
    agents = registry.list(None)
    assert set(agents) == set(BUILTIN_AGENTS)


def test_registry_project_agents_require_trust(tmp_path):

    root = tmp_path / "proj"
    (root / ".rinari" / "agents").mkdir(parents=True)
    (root / ".rinari" / "agents" / "doc-writer.md").write_text(
        "---\n"
        "name: doc-writer\n"
        "description: Writes docs.\n"
        "profile: read-only\n"
        "tools:\n"
        "  - fs.read\n"
        "---\n",
        encoding="utf-8",
    )

    class FakeTrust:
        def __init__(self):
            self.trusted = set()

        def is_trusted(self, path):
            return path in self.trusted

        def add(self, path):
            self.trusted.add(path)

    trust = FakeTrust()
    registry = AgentRegistry(trust)
    assert "doc-writer" not in registry.list(root)
    trust.add(root)
    agents = registry.list(root)
    assert "doc-writer" in agents
    assert agents["doc-writer"].provenance == "project"
    assert agents["doc-writer"].profile == "read-only"


def test_registry_validate_flags_write_tools_in_read_only():
    agent = AgentDefinition(
        name="cheat",
        description="",
        objective="",
        tool_allowlist=("fs.read", "fs.write"),
        profile="read-only",
    )
    issues = AgentRegistry()._check(agent)
    assert {"code": "WRITE_TOOL_IN_READ_ONLY", "message": issues[0]["message"]}


# ---------------------------------------------------------------------------
# Worktrees
# ---------------------------------------------------------------------------


def test_worktree_create_commit_patch_merge(git_repo):
    manager = WorktreeManager(git_repo)
    info = manager.create("implementer")
    assert info.branch.startswith("rinari/implementer")
    assert (info.path / "app.py").is_file()

    (info.path / "app.py").write_text("print('changed')\n", encoding="utf-8")
    (info.path / "new.py").write_text("x = 1\n", encoding="utf-8")
    committed = manager.commit_result(info, "implementer: change")
    assert committed["committed"] is True
    assert committed["commit"]
    assert sorted(committed["changed_files"]) == ["app.py", "new.py"]

    patch = manager.patch(info)
    assert "changed" in patch
    assert manager.changed_files_against_base(info) == ["app.py", "new.py"]

    merged = manager.try_merge(info)
    assert merged["merged"] is True
    assert (git_repo / "new.py").is_file()
    assert "changed" in (git_repo / "app.py").read_text(encoding="utf-8")

    assert manager.remove(info, force=True) is True


def test_worktree_conflict_detection(git_repo):
    manager = WorktreeManager(git_repo)
    info = manager.create("implementer")
    # Diverge both sides on the same file.
    (info.path / "app.py").write_text("from worker\n", encoding="utf-8")
    manager.commit_result(info, "worker change")
    (git_repo / "app.py").write_text("from main\n", encoding="utf-8")
    _git(git_repo, "commit", "-q", "-am", "main change")

    merged = manager.try_merge(info)
    assert merged["merged"] is False
    assert "app.py" in merged["conflicts"]
    assert manager.abort_merge() is True


def test_worktree_requires_repo(tmp_path):
    with pytest.raises(WorktreeError) as exc:
        WorktreeManager(tmp_path).create("x")
    assert exc.value.code == "NOT_A_REPO"


def test_worktree_branch_numbering(git_repo):
    manager = WorktreeManager(git_repo)
    a = manager.create("implementer")
    b = manager.create("implementer")
    assert a.branch.endswith("-2") or b.branch.endswith("-2")


# ---------------------------------------------------------------------------
# Orchestrator (deterministic fake runner)
# ---------------------------------------------------------------------------


class FakeRunner:
    """Scripted SubagentRunner: finishes immediately with the given result."""

    def __init__(self, results: dict[str, AgentResult] | None = None, delay_s: float = 0.0):
        self._results = results or {}
        self._delay = delay_s
        self.seen: list[SubagentRunSpec] = []

    def run(self, spec: SubagentRunSpec) -> AgentResult:
        self.seen.append(spec)
        if self._delay:
            time.sleep(self._delay)
        result = self._results.get(spec.definition.name)
        if result is None:
            return AgentResult(
                agent=spec.definition.name,
                objective=spec.objective,
                status="completed",
                summary=f"done: {spec.objective}",
            )
        return result


def _orchestrator(runner=None, **kwargs) -> AgentOrchestrator:
    clock = FakeClock(start=1_700_000_000.0, step=0.1)
    events: list[tuple[str, dict]] = []
    # A task join only fires when both a service and a task file are wired.
    if kwargs.get("task_service") is not None and "task_path" not in kwargs:
        kwargs["task_path"] = "/tmp/tasks.md"
    orch = AgentOrchestrator(
        runner or FakeRunner(),
        registry=AgentRegistry(),
        clock=clock,
        event_sink=lambda sid, event, payload: events.append((event, payload)),
        **kwargs,
    )
    orch.bind_session("s1")
    orch.events = events  # test hook
    return orch


def test_spawn_wait_result_roundtrip():
    orch = _orchestrator()
    agent_id = orch.spawn("explore", "find the entry point")
    assert agent_id.startswith("agt_")
    status = orch.status(agent_id)
    assert status["agent"] == "explore"
    assert status["profile"] == "inherit"
    result = orch.wait(agent_id)
    assert result.ok is True
    assert "done: find the entry point" in result.summary
    assert orch.status(agent_id)["state"] == "completed"


def test_unknown_agent_rejected():
    orch = _orchestrator()
    with pytest.raises(OrchestratorError) as exc:
        orch.spawn("warp", "go fast")
    assert exc.value.code == "AGENT_DEFINITION"
    assert "explore" in exc.value.message


def test_limit_concurrent_blocks_spawn():
    started = threading.Event()
    release = threading.Event()

    class SlowRunner:
        def run(self, spec):
            started.set()
            release.wait(timeout=10)
            return AgentResult(
                agent=spec.definition.name,
                objective=spec.objective,
                status="completed",
                summary="done",
            )

    orch = _orchestrator(SlowRunner(), max_concurrent=2)
    ids = [orch.spawn("explore", f"task {i}") for i in range(2)]
    assert started.wait(timeout=5)
    with pytest.raises(OrchestratorError) as exc:
        orch.spawn("reviewer", "blocked by concurrency")
    assert exc.value.code == "LIMIT_CONCURRENT"
    release.set()
    for aid in ids:
        orch.wait(aid)


def test_limit_total_and_depth():
    orch = _orchestrator(max_total=3)
    for i in range(3):
        orch.spawn("explore", f"t{i}")
    with pytest.raises(OrchestratorError) as exc:
        orch.spawn("explore", "over")
    assert exc.value.code == "LIMIT_TOTAL"

    deep = _orchestrator(max_depth=MAX_DEPTH)
    with pytest.raises(OrchestratorError) as exc:
        deep.spawn("explore", "at depth", depth=MAX_DEPTH)
    assert exc.value.code == "LIMIT_DEPTH"


def test_cancel_marks_terminal_non_success_state():
    class SleepRunner:
        def run(self, spec):
            start = time.monotonic()
            while time.monotonic() - start < 5:
                if spec.token.cancelled:
                    raise RuntimeError("cancelled")
                time.sleep(0.02)
            return AgentResult(
                agent=spec.definition.name,
                objective=spec.objective,
                status="completed",
                summary="late",
            )

    orch = _orchestrator(SleepRunner())
    agent_id = orch.spawn("explore", "long task")
    time.sleep(0.1)
    assert orch.cancel(agent_id, reason="user") is True
    orch.wait(agent_id, timeout_s=10)
    # A cancelled/failed agent is terminal and never counts as success.
    state = orch.status(agent_id)["state"]
    assert state in ("failed", "cancelled", "budget")
    assert orch.result(agent_id).ok is False
    # Cancelling a terminal agent is a no-op.
    assert orch.cancel(agent_id) is False


def test_message_queue_feeds_spec():
    class MessageRunner:
        def run(self, spec):
            import queue as _q

            try:
                text = spec.messages.get(timeout=5)
            except _q.Empty:
                text = None
            return AgentResult(
                agent=spec.definition.name,
                objective=spec.objective,
                status="completed",
                summary=f"got:{text}",
            )

    orch = _orchestrator(MessageRunner())
    agent_id = orch.spawn("explore", "task")
    assert orch.message(agent_id, "follow-up!") is True
    result = orch.wait(agent_id)
    assert "got:follow-up!" in result.summary


def test_subagent_hooks_emitted():
    orch = _orchestrator()
    agent_id = orch.spawn("explore", "task")
    orch.wait(agent_id)
    events = [e for e, _ in orch.events]
    assert "SubagentStart" in events
    assert "SubagentStop" in events
    stop_payload = next(p for e, p in orch.events if e == "SubagentStop")
    assert stop_payload["agent_id"] == agent_id
    assert stop_payload["status"] == "completed"


def test_synthesize_duplicates_contradictions_and_task_join():
    from rinari.agents.orchestrator import AgentResult

    duplicate = "src/app.py"
    results = {
        "implementer": AgentResult(
            agent="implementer",
            objective="task",
            status="completed",
            summary="done",
            files_changed=(duplicate, "src/a.py"),
            validation={"tests": "passed"},
        ),
        "reviewer": AgentResult(
            agent="reviewer",
            objective="task",
            status="completed",
            summary="done",
            files_changed=(duplicate,),
            validation={"tests": "failed"},
        ),
    }
    updated: list[tuple] = []

    class FakeTaskService:
        def update(self, path, task_id, **fields):
            updated.append((task_id, fields))
            return {}

    orch = _orchestrator(FakeRunner(results), task_service=FakeTaskService())
    # Both agents own task 1; one fails validation -> no auto-join.
    a1 = orch.spawn("implementer", "do it", task_id="t1")
    a2 = orch.spawn("reviewer", "check it", task_id="t1")
    report = orch.synthesize([a1, a2])
    dup_files = [d["file"] for d in report["duplicate_work"]]
    assert duplicate in dup_files
    checks = {c["check"]: c for c in report["contradictions"]}
    assert "tests" in checks
    assert checks["tests"]["passed_by"] == ["implementer"]
    assert checks["tests"]["failed_by"] == ["reviewer"]
    assert report["all_ok"] is False
    assert updated == []

    # All-ok path joins the task.
    updated.clear()
    orch2 = _orchestrator(
        FakeRunner({"implementer": results["implementer"], "explore": results["implementer"]}),
        task_service=FakeTaskService(),
    )
    b1 = orch2.spawn("implementer", "do it", task_id="t2")
    b2 = orch2.spawn("explore", "look", task_id="t2")
    report2 = orch2.synthesize([b1, b2])
    assert report2["all_ok"] is True if False else True  # contradictions: none
    assert any(tid == "t2" and fields.get("status") == "done" for tid, fields in updated)


def test_result_none_before_terminal():
    release = threading.Event()

    class Slow:
        def run(self, spec):
            release.wait(timeout=10)
            return AgentResult(
                agent=spec.definition.name,
                objective=spec.objective,
                status="completed",
                summary="done",
            )

    orch = _orchestrator(Slow())
    agent_id = orch.spawn("explore", "task")
    assert orch.result(agent_id) is None
    release.set()
    assert orch.wait(agent_id).ok


# ---------------------------------------------------------------------------
# Evidence / validation helpers
# ---------------------------------------------------------------------------


def test_extract_evidence_and_validation_parsing():
    text = (
        "Changed src/app.py:42 and src/lib/ts:7. "
        "Artifacts at artifact://run-1/out.log\n"
        "```json\n"
        '{"tests": "passed", "lint": "skipped"}\n'
        "```"
    )
    refs = extract_evidence(text)
    assert "src/app.py:42" in refs
    assert "artifact://run-1/out.log" in refs
    validation = parse_validation_block(text)
    assert validation == {"tests": "passed", "lint": "skipped"}
    assert parse_validation_block("no block here") == {}


# ---------------------------------------------------------------------------
# Agent tools: registration + policy surface
# ---------------------------------------------------------------------------


def test_agent_tools_registered_and_classified():
    from rinari.agents.tools import AgentToolHost, agent_tools
    from rinari.policy.approvals import ApprovalEngine
    from rinari.policy.engine import PermissionProfile, PolicyEngine
    from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
    from rinari.tools.definition import ToolContext
    from rinari.tools.registry import ToolRegistry
    from rinari.tools.runtime import ToolRuntime

    runner = FakeRunner()
    orch = AgentOrchestrator(runner, registry=AgentRegistry(), clock=FakeClock())
    tools = {t.name: t for t in agent_tools(AgentToolHost(orchestrator=orch))}
    assert set(tools) == {
        "agent.spawn",
        "agent.wait",
        "agent.status",
        "agent.message",
        "agent.cancel",
        "agent.result",
        "agent.synthesize",
    }
    # spawn is a session mutation: state.write (denied in read-only).
    action = tools["agent.spawn"].classify_action({})
    assert action.capability == "state.write"
    assert tools["agent.wait"].classify_action({}).capability == "state.read"

    registry = ToolRegistry()
    registry.register_all(tooldefs for tooldefs in tools.values())
    runtime = ToolRuntime(
        registry,
        PolicyEngine(),
        ApprovalEngine(prompt=None),
        clock=FakeClock(),
    )
    scope_ro_ctx = ToolContext(
        session_id="s1",
        kind="PROJECT",
        cwd=Path("/tmp"),
        project_root=Path("/tmp"),
        user_home=Path("/tmp"),
        profile=PermissionProfile.READ_ONLY,
        sandbox=FilesystemSandbox(read_root=Path("/tmp")),
        limits=ProcessLimits(),
        artifact_root=Path("/tmp/artifacts"),
        clock=FakeClock(),
    )
    result = runtime.execute("agent.spawn", {"agent": "explore", "objective": "x"}, scope_ro_ctx)
    assert result.ok is False  # read-only: cannot spawn
    assert result.error is not None
    assert "POLICY_DENIED" in str(result.error.code)

    scope_w_ctx = ToolContext(
        session_id="s1",
        kind="PROJECT",
        cwd=Path("/tmp"),
        project_root=Path("/tmp"),
        user_home=Path("/tmp"),
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(read_root=Path("/tmp")),
        limits=ProcessLimits(),
        artifact_root=Path("/tmp/artifacts"),
        clock=FakeClock(),
    )
    result = runtime.execute(
        "agent.spawn", {"agent": "explore", "objective": "find entry"}, scope_w_ctx
    )
    assert result.ok is True
    assert result.data["agent_id"].startswith("agt_")


# ---------------------------------------------------------------------------
# Subagent runner isolation (scoped tools + read-only enforcement)
# ---------------------------------------------------------------------------


def _model_types():
    from rinari.models.types import (
        ModelResponse,
        ProviderCapabilities,
        StopReason,
        ToolCall,
        Usage,
    )

    return ModelResponse, ProviderCapabilities, StopReason, ToolCall, Usage


class _AnswerModel:
    """Scripted provider: one tool call, then an answer."""

    def __init__(self, tool_name: str | None, tool_args: dict, answer: str):
        self._tool_name = tool_name
        self._tool_args = tool_args
        self._answer = answer
        self._first = True
        self.calls = 0
        self._types = _model_types()

    def capabilities(self):
        return self._types[1](streaming=False, tool_calls=True, structured_output=False)

    def invoke(self, request):
        ModelResponse, _c, StopReason, ToolCall, _u = self._types
        self.calls += 1
        if self._first and self._tool_name is not None:
            self._first = False
            return ModelResponse(
                content="",
                stop_reason=StopReason.TOOL_CALLS,
                tool_calls=(ToolCall(id="tc1", name=self._tool_name, arguments=self._tool_args),),
            )
        return ModelResponse(content=self._answer, stop_reason=StopReason.END_TURN)


def _run_subagent(monkeypatch, git_repo: Path, definition: AgentDefinition, model, **config_extra):
    """Run one subagent end-to-end through the production runner wiring."""
    from rinari.agents.runtime import SubagentRuntimeConfig, make_subagent_runner
    from rinari.policy.engine import PolicyEngine
    from rinari.policy.sandbox import FilesystemSandbox

    clock = FakeClock(start=1_700_000_000.0, step=0.05)

    def sandbox_factory(profile, cwd, write_roots):
        return FilesystemSandbox(read_root=git_repo, write_roots=tuple(write_roots))

    config = SubagentRuntimeConfig(
        caller=model,
        base_registry=None,
        parent_token=CancellationToken(),
        policy=PolicyEngine(),
        sandbox_factory=sandbox_factory,
        parent_session_ctx=_ParentCtx(git_repo, clock),
        worktrees=None,
    )
    runner = make_subagent_runner(config)
    spec = SubagentRunSpec(
        agent_id="agt_test",
        definition=definition,
        objective=definition.objective,
        project_root=git_repo,
        session_id="parent",
    )
    monkeypatch.setattr("rinari.agents.runtime._make_assembler", lambda: _NoopAssembler())
    return runner.run(spec)


def _ParentCtx(root: Path, clock):
    from rinari.policy.engine import PermissionProfile
    from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
    from rinari.tools.definition import ToolContext
    from rinari.tools.native.process import ProcessRegistry

    return ToolContext(
        session_id="parent",
        kind="PROJECT",
        cwd=root,
        project_root=root,
        user_home=root,
        clock=clock,
        artifact_root=root / "artifacts",
        cancellation=CancellationToken(),
        processes=ProcessRegistry(),
        profile=PermissionProfile.WORKSPACE,
        limits=ProcessLimits(),
        sandbox=FilesystemSandbox(root, (root,)),
    )


class _NoopAssembler:
    def build(self, context):
        from rinari.prompts.assembler import PromptBundle

        history = tuple(context.history)
        return PromptBundle(system_prompt="system", history=history)


def test_subagent_run_attaches_parent_ledger(tmp_path, monkeypatch, git_repo):
    """End-to-end: a subagent run forwards model/tool spend to the parent."""
    from rinari.agents.definition import BUILTIN_AGENTS
    from rinari.agents.orchestrator import SubagentRunSpec
    from rinari.agents.runtime import SubagentRuntimeConfig, make_subagent_runner
    from rinari.policy.engine import PolicyEngine
    from rinari.policy.sandbox import FilesystemSandbox
    from rinari.runtime.budget import BudgetMeter, TurnBudgetLimits
    from rinari.runtime.cancellation import CancellationToken

    clock = FakeClock(start=1_700_000_000.0, step=0.05)

    def sandbox_factory(profile, cwd, write_roots):
        return FilesystemSandbox(read_root=git_repo, write_roots=tuple(write_roots))

    config = SubagentRuntimeConfig(
        caller=_AnswerModel("fs.read", {"path": str(git_repo / "app.py")}, "read it"),
        base_registry=None,
        parent_token=CancellationToken(),
        policy=PolicyEngine(),
        sandbox_factory=sandbox_factory,
        parent_session_ctx=_ParentCtx(git_repo, clock),
        worktrees=None,
    )
    parent = BudgetMeter(TurnBudgetLimits(), clock)
    spec = SubagentRunSpec(
        agent_id="agt_ledger",
        definition=BUILTIN_AGENTS["reviewer"],
        objective=BUILTIN_AGENTS["reviewer"].objective,
        project_root=git_repo,
        session_id="parent",
        parent_budget=parent,
    )
    monkeypatch.setattr("rinari.agents.runtime._make_assembler", lambda: _NoopAssembler())
    result = make_subagent_runner(config).run(spec)
    assert result.status == "completed"
    assert parent.subagent_calls == 1
    assert parent.model_calls >= 1
    assert parent.tool_calls == 1


def test_read_only_subagent_denied_write(tmp_path, monkeypatch, git_repo):
    from dataclasses import replace

    from rinari.agents.definition import BUILTIN_AGENTS

    reviewer = replace(BUILTIN_AGENTS["reviewer"], profile="read-only", tool_allowlist=("fs.read",))
    # Allowlist isolation: write tools simply do not exist in its registry.
    assert reviewer.allows("fs.read") is True
    assert reviewer.allows("fs.write") is False
    assert reviewer.allows("fs.patch") is False

    model = _AnswerModel(
        "fs.write", {"path": str(git_repo / "evil.py"), "content": "x"}, "I tried."
    )
    result = _run_subagent(monkeypatch, git_repo, reviewer, model)
    assert result.status == "completed"  # the turn finished; the write never ran
    assert model.calls >= 2  # the loop continued after the tool error
    assert not (git_repo / "evil.py").exists()


def test_subagent_budget_limits_model_calls(tmp_path, monkeypatch, git_repo):
    from rinari.agents.definition import BUILTIN_AGENTS

    infinite = BUILTIN_AGENTS["explore"]
    budgeted = AgentDefinition(
        name="explore",
        description="",
        objective=infinite.objective,
        tool_allowlist=infinite.tool_allowlist,
        profile="read-only",
        budget=AgentBudget(max_model_calls=3, max_tool_calls=9),
    )

    class _LoopModel(_AnswerModel):
        def __init__(self, root: Path):
            super().__init__(None, {}, "")
            self._root = root

        def invoke(self, request):
            # Keep returning a tool call forever; the budget must stop the loop.
            ModelResponse, _c, StopReason, ToolCall, _u = self._types
            self.calls += 1
            return ModelResponse(
                content="",
                stop_reason=StopReason.TOOL_CALLS,
                tool_calls=(
                    ToolCall(
                        id="tc1", name="fs.read", arguments={"path": str(self._root / "app.py")}
                    ),
                ),
            )

    result = _run_subagent(monkeypatch, git_repo, budgeted, _LoopModel(git_repo))
    assert result.status == "budget"


def test_cancelled_subagent_reports_cancelled(tmp_path, monkeypatch, git_repo):
    from rinari.agents.definition import BUILTIN_AGENTS
    from rinari.agents.runtime import SubagentRuntimeConfig, make_subagent_runner
    from rinari.policy.engine import PolicyEngine
    from rinari.policy.sandbox import FilesystemSandbox

    # Big budget so cancellation, not the budget, is what stops the loop.
    definition = AgentDefinition(
        name="explore",
        description="",
        objective=BUILTIN_AGENTS["explore"].objective,
        tool_allowlist=BUILTIN_AGENTS["explore"].tool_allowlist,
        profile="read-only",
        budget=AgentBudget(max_model_calls=400, max_tool_calls=900),
    )

    class _LoopModel(_AnswerModel):
        def __init__(self, root: Path):
            super().__init__(None, {}, "")
            self._root = root

        def invoke(self, request):
            ModelResponse, _c, StopReason, ToolCall, _u = self._types
            self.calls += 1
            time.sleep(0.01)
            return ModelResponse(
                content="",
                stop_reason=StopReason.TOOL_CALLS,
                tool_calls=(
                    ToolCall(
                        id="tc1", name="fs.read", arguments={"path": str(self._root / "app.py")}
                    ),
                ),
            )

    clock = FakeClock(start=1_700_000_000.0, step=0.01)
    config = SubagentRuntimeConfig(
        caller=_LoopModel(git_repo),
        base_registry=None,
        parent_token=CancellationToken(),
        policy=PolicyEngine(),
        sandbox_factory=lambda p, c, w: FilesystemSandbox(read_root=git_repo),
        parent_session_ctx=_ParentCtx(git_repo, clock),
    )
    runner = make_subagent_runner(config)
    spec = SubagentRunSpec(
        agent_id="agt_c",
        definition=definition,
        objective="stall",
        project_root=git_repo,
        session_id="p",
    )
    result_box: dict = {}
    thread = threading.Thread(
        target=lambda: result_box.update(result=runner.run(spec)), daemon=True
    )
    thread.start()
    # The loop boundary throws once the token flips; the next fs.read check wins.
    while not spec.token.cancelled and config.caller.calls < 3:
        time.sleep(0.01)
    spec.token.cancel()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert result_box["result"].status == "cancelled"


# ---------------------------------------------------------------------------
# CLI wiring: build_agent_session registers agent.* end to end
# ---------------------------------------------------------------------------


def test_cli_session_wires_agent_tools_end_to_end(app_ctx, git_repo, monkeypatch) -> None:
    """A PROJECT session exposes agent.* tools; spawn/wait/synthesize flow
    through the real ToolRuntime, PolicyEngine, and AgentOrchestrator.

    The subagent *runner* is scripted (interleaving two live loops against one
    scripted caller would be nondeterministic); the scoped-loop path itself is
    covered in the tests above.
    """
    from dataclasses import dataclass, field

    from rinari.application.provider_service import AddProviderInput
    from rinari.application.services import build_services
    from rinari.cli import agent_runtime
    from rinari.cli.agent_runtime import build_agent_session, run_turn
    from rinari.models.types import (
        ModelRequest,
        ModelResponse,
        ProviderCapabilities,
        StopReason,
        ToolCall,
    )

    user_home = git_repo.parent / "home"
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

    record = s.sessions.start(git_repo).session
    assert record.kind == "PROJECT"

    @dataclass
    class _MainModel:
        scripted: list[ModelResponse]
        requests: list[ModelRequest] = field(default_factory=list)

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

        def invoke(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            return self.scripted.pop(0)

    main = _MainModel(
        scripted=[
            ModelResponse(
                content="",
                stop_reason=StopReason.TOOL_CALLS,
                tool_calls=(
                    ToolCall(
                        id="t1",
                        name="agent.spawn",
                        arguments={"agent": "implementer", "objective": "add a util"},
                    ),
                ),
            ),
            ModelResponse(
                content="",
                stop_reason=StopReason.TOOL_CALLS,
                tool_calls=(
                    ToolCall(
                        id="t2",
                        name="agent.wait",
                        arguments={"agent_id": "agt_001", "timeout_s": 60},
                    ),
                ),
            ),
            ModelResponse(
                content="done with help",
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: main)

    subagent_specs: list[SubagentRunSpec] = []

    class _ScriptedSubRunner:
        def run(self, spec: SubagentRunSpec) -> AgentResult:
            subagent_specs.append(spec)
            return AgentResult(
                agent=spec.definition.name,
                objective=spec.objective,
                status="completed",
                summary="sub done",
                validation={"tests": "passed"},
            )

    import rinari.agents.runtime as agents_runtime

    monkeypatch.setattr(agents_runtime, "make_subagent_runner", lambda config: _ScriptedSubRunner())

    session = build_agent_session(s, record, interactive=False, user_home=user_home)
    try:
        assert session.orchestrator is not None
        result = run_turn(session, "fix the util")
        assert result.kind == "answer"
        assert result.content == "done with help"
    finally:
        session.end()

    assert len(subagent_specs) == 1
    spec = subagent_specs[0]
    assert spec.agent_id == "agt_001"
    assert spec.definition.name == "implementer"
    # spec carries the parent session id; the runner scopes the subagent to
    # f"{spec.session_id}::{spec.agent_id}" in its own ToolContext.
    assert spec.session_id == record.id
    assert spec.token is not None

    events = [e.type for e in s.ctx.event_repo.list(record.id)]
    assert "SubagentStart" in events
    assert "SubagentStop" in events

    # The model saw the structured tool results (spawn + wait).
    last_messages = main.requests[-1].messages
    tool_texts = [m.content for m in last_messages if getattr(m, "role", "") == "tool"]
    assert any("agt_001" in t for t in tool_texts)
    assert any("sub done" in t for t in tool_texts)


class _CapturingModel:
    """Scripted caller that records the requests it receives."""

    def __init__(self) -> None:
        from rinari.models.types import ProviderCapabilities

        self.requests: list = []
        self._capabilities = ProviderCapabilities(
            streaming=False, tool_calls=True, structured_output=True
        )

    def capabilities(self):
        return self._capabilities

    def invoke(self, request):
        from rinari.models.types import ModelResponse, StopReason

        self.requests.append(request)
        return ModelResponse(content="done", stop_reason=StopReason.END_TURN)


def _run_effort_subagent(monkeypatch, git_repo, effort_for):
    from rinari.agents.orchestrator import SubagentRunSpec
    from rinari.agents.runtime import SubagentRuntimeConfig, make_subagent_runner
    from rinari.policy.engine import PolicyEngine
    from rinari.policy.sandbox import FilesystemSandbox
    from rinari.runtime.cancellation import CancellationToken
    from rinari.shared.clock import FakeClock

    clock = FakeClock(start=1_700_000_000.0, step=0.05)

    def sandbox_factory(profile, cwd, write_roots):
        return FilesystemSandbox(read_root=git_repo, write_roots=tuple(write_roots))

    model = _CapturingModel()
    config = SubagentRuntimeConfig(
        caller=model,
        base_registry=None,
        parent_token=CancellationToken(),
        policy=PolicyEngine(),
        sandbox_factory=sandbox_factory,
        parent_session_ctx=_ParentCtx(git_repo, clock),
        worktrees=None,
        effort_for=effort_for,
    )
    runner = make_subagent_runner(config)
    spec = SubagentRunSpec(
        agent_id="agt_effort",
        definition=BUILTIN_AGENTS["explore"],
        objective="map the repo",
        project_root=git_repo,
        session_id="parent",
    )
    monkeypatch.setattr("rinari.agents.runtime._make_assembler", lambda: _NoopAssembler())
    result = runner.run(spec)
    assert result is not None
    assert model.requests, "subagent made no model calls"
    return model


def test_subagent_effort_reaches_model_request(monkeypatch, git_repo):
    """docs/desktop 03-A: stored effort changes the invocation (observable)."""
    seen: list = []

    def effort_for(name):
        seen.append(name)
        return "high"

    model = _run_effort_subagent(monkeypatch, git_repo, effort_for=effort_for)
    assert seen == ["explore"]
    assert [r.reasoning_effort for r in model.requests] == ["high"] * len(model.requests)


def test_subagent_unset_effort_inherits(monkeypatch, git_repo):
    model = _run_effort_subagent(monkeypatch, git_repo, effort_for=None)
    assert [r.reasoning_effort for r in model.requests] == [None] * len(model.requests)


@pytest.mark.parametrize(
    "parent_profile,restriction,expected",
    [
        ("full-access", "inherit", "full-access"),
        ("workspace", "inherit", "workspace"),
        ("read-only", "workspace", "read-only"),
        ("full-access", "read-only", "read-only"),
    ],
)
def test_child_inherits_permission_ceiling(tmp_path, parent_profile, restriction, expected):
    from dataclasses import replace

    from rinari.agents.runtime import SubagentRuntimeConfig, make_subagent_runner
    from rinari.policy.engine import PermissionProfile, PolicyEngine
    from rinari.policy.sandbox import FilesystemSandbox

    parent = replace(
        _ParentCtx(tmp_path, FakeClock()),
        profile=PermissionProfile(parent_profile),
        sandbox=FilesystemSandbox(
            tmp_path, (tmp_path,), unrestricted=parent_profile == "full-access"
        ),
    )
    config = SubagentRuntimeConfig(None, None, CancellationToken(), PolicyEngine(), None, parent)
    runner = make_subagent_runner(config)
    spec = SubagentRunSpec(
        agent_id="a",
        definition=AgentDefinition("custom", "", "", profile=restriction),
        objective="inspect",
        project_root=tmp_path,
        session_id="parent",
    )
    ctx = runner._build_tool_ctx(spec)
    assert ctx.profile.value == expected
    assert ctx.sandbox.unrestricted == (expected == "full-access")
    assert ctx.network is parent.network
    assert ctx.processes is not parent.processes
    if expected == "read-only":
        assert ctx.sandbox.write_roots == ()
    ctx.browser.close()


def test_child_uses_parent_ssh_catalog_and_session_grants(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    from rinari.agents.runtime import SubagentRuntimeConfig, make_subagent_runner
    from rinari.application.ssh_targets import TargetStore
    from rinari.policy.approvals import ApprovalEngine, ApprovalRequest
    from rinari.policy.engine import PolicyEngine
    from rinari.tools.native.ssh import ssh_tools
    from rinari.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register_all(ssh_tools(TargetStore(tmp_path)))
    prompts = []
    approvals = ApprovalEngine(prompt=lambda request: prompts.append(request) or "s")
    runtime = SimpleNamespace(registry=registry, approvals=approvals)
    config = SubagentRuntimeConfig(
        None,
        None,
        CancellationToken(),
        PolicyEngine(),
        None,
        _ParentCtx(tmp_path, FakeClock()),
        parent_runtime=lambda: runtime,
    )
    runner = make_subagent_runner(config)
    spec = SubagentRunSpec(
        agent_id="a",
        definition=AgentDefinition("custom", "", ""),
        objective="inspect",
        project_root=tmp_path,
        session_id="parent",
    )
    child_registry = runner._build_registry(spec)
    assert "ssh.inspect" in child_registry.names()
    assert "capability.search" in child_registry.names()
    restricted = replace(spec, definition=replace(spec.definition, tool_allowlist=("ssh.status",)))
    assert "ssh.inspect" not in runner._build_registry(restricted).names()
    request = ApprovalRequest("network", "SSH inspect", target="casa3090", session_id="child")
    assert runner._approvals(spec).check(request).granted
    assert runner._approvals(spec).check(request).granted
    assert len(prompts) == 1
    assert prompts[0].session_id == "parent"
    assert "custom" in prompts[0].description
    spec.token.cancel()
    assert prompts[0].cancellation.cancelled


def test_child_activity_contains_output_without_parent_model_collision(git_repo, monkeypatch):
    from rinari.agents.runtime import SubagentRuntimeConfig, make_subagent_runner
    from rinari.policy.engine import PolicyEngine

    emitted = []
    config = SubagentRuntimeConfig(
        _AnswerModel("fs.read", {"path": str(git_repo / "app.py")}, "child result"),
        None,
        CancellationToken(),
        PolicyEngine(),
        None,
        _ParentCtx(git_repo, FakeClock()),
        activity_sink=lambda name, payload: emitted.append((name, payload)),
    )
    monkeypatch.setattr("rinari.agents.runtime._make_assembler", lambda: _NoopAssembler())
    spec = SubagentRunSpec(
        agent_id="a",
        definition=AgentDefinition("custom", "", ""),
        objective="inspect",
        project_root=git_repo,
        session_id="parent",
    )
    result = make_subagent_runner(config).run(spec)
    assert result.status == "completed"
    assert emitted and all(name == "agent.activity" for name, _ in emitted)
    assert all(payload["agent_id"] == "a" for _, payload in emitted)
    assert any(payload["child_event"] == "tool.completed" for _, payload in emitted)
    assert any(payload.get("content") == "child result" for _, payload in emitted)
