"""CHAT -> PROJECT promotion flow: candidate workspace, in-session detection,
permission recalculation (phase-2 closeout of the phase-1 promotion items)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
import typer

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.cli.agent_runtime import _sandbox_for, build_agent_session, run_turn
from rinari.models.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
    ToolCall,
)
from rinari.policy.engine import (
    CAPABILITY_FS_WRITE,
    PolicyAction,
    PolicyEngine,
    SessionScope,
    normalize_profile,
)


@dataclass
class FakeModel:
    scripted: list[ModelResponse]
    requests: list[ModelRequest] = field(default_factory=list)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.scripted.pop(0)


@pytest.fixture
def env(app_ctx, tmp_path):
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
    return tmp_path, user_home, s


def _start_chat(s, cwd: Path, user_home: Path):
    started = s.sessions.start(Path(cwd), forced_chat=True)
    return started.session


def _tool_call_response() -> ModelResponse:
    return ModelResponse(
        content="",
        tool_calls=(ToolCall(id="tc1", name="shell.exec", arguments={"command": "git init"}),),
        stop_reason=StopReason.TOOL_CALLS,
    )


def test_chat_candidate_workspace_writes_allowed(env) -> None:
    tmp_path, user_home, s = env
    workspace = tmp_path / "proj"
    workspace.mkdir()
    record = _start_chat(s, workspace, user_home)
    assert record.kind == "CHAT"

    sandbox = _sandbox_for(record, user_home)
    assert sandbox.write_roots == (Path(workspace).resolve(),)

    scope = SessionScope(
        kind="CHAT",
        root=workspace,
        cwd=workspace,
        profile=normalize_profile("workspace"),
        user_home=user_home,
    )
    decision = PolicyEngine().decide(CAPABILITY_FS_WRITE, scope, path="notes.md")
    assert decision.action is PolicyAction.ALLOW
    assert "candidate project workspace" in decision.reason


def test_home_candidate_workspace_stays_locked(env) -> None:
    _, user_home, s = env
    (user_home / "newproj").mkdir()
    record = _start_chat(s, user_home / "newproj", user_home)

    sandbox = _sandbox_for(record, user_home)
    # A project subdirectory under home is a fine candidate workspace.
    assert sandbox.write_roots == (Path(user_home / "newproj").resolve(),)

    home_record = s.sessions.start(user_home, forced_chat=True).session
    home_record.state = record.state
    home_sandbox = _sandbox_for(home_record, user_home)
    assert home_sandbox.write_roots == ()

    scope = SessionScope(
        kind="CHAT",
        root=user_home,
        cwd=user_home,
        profile=normalize_profile("workspace"),
        user_home=user_home,
    )
    decision = PolicyEngine().decide(CAPABILITY_FS_WRITE, scope, path=".rinarenc")
    assert decision.action is PolicyAction.ASK  # never silent-allowed at $HOME


def test_agent_git_init_promotes_session_in_place(env, monkeypatch) -> None:
    tmp_path, user_home, s = env
    workspace = tmp_path / "newproj"
    workspace.mkdir()
    record = _start_chat(s, workspace, user_home)

    fake = FakeModel(
        scripted=[
            _tool_call_response(),
            ModelResponse(content="Done.", stop_reason=StopReason.END_TURN),
            ModelResponse(content="still here", stop_reason=StopReason.END_TURN),
        ]
    )
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    monkeypatch.setattr(typer, "prompt", lambda *args, **kwargs: "y")

    session = build_agent_session(s, record, interactive=True, user_home=user_home)
    result = run_turn(session, "Create a git project here")
    assert result.kind == "answer"
    assert (workspace / ".git").exists()

    # Record promoted...
    reloaded = s.ctx.session_repo.get(record.id)
    assert reloaded.kind == "PROJECT"
    assert Path(reloaded.project_root_snapshot) == Path(workspace).resolve()
    assert reloaded.provider_id == record.provider_id
    assert reloaded.model_id == record.model_id

    # ...and recalculated in process (permisos + contexto)...
    assert session.record.kind == "PROJECT"
    assert session.context.tool_ctx.kind == "PROJECT"
    assert session.context.tool_ctx.project_root == Path(workspace).resolve()
    assert session.context.tool_ctx.sandbox.write_roots == (Path(workspace).resolve(),)
    assert session.promoted_root == Path(workspace).resolve()

    events = [e.type for e in s.ctx.event_repo.list(record.id)]
    assert "SessionPromotedToProject" in events
    assert "SessionPromotedInProcess" in events

    # Conversation survived promotion: the next turn sees the full history.
    result2 = run_turn(session, "status?")
    assert result2.kind == "answer"
    last_messages = fake.requests[-1].messages
    assert any(m.content == "Create a git project here" for m in last_messages)
    assert any(m.content == "Done." for m in last_messages)


def test_forced_chat_inside_repo_not_promoted_by_walkup(env, monkeypatch) -> None:
    tmp_path, user_home, s = env
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()

    record = _start_chat(s, sub, user_home)
    fake = FakeModel(scripted=[ModelResponse(content="ok", stop_reason=StopReason.END_TURN)])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    session = build_agent_session(s, record, interactive=False, user_home=user_home)
    run_turn(session, "what is this?")

    reloaded = s.ctx.session_repo.get(record.id)
    assert reloaded.kind == "CHAT"
    assert reloaded.project_root_snapshot is None
    assert session.promoted_root is None


def test_no_marker_no_promotion(env, monkeypatch) -> None:
    tmp_path, user_home, s = env
    workspace = tmp_path / "plain"
    workspace.mkdir()
    record = _start_chat(s, workspace, user_home)
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")
    fake = FakeModel(scripted=[ModelResponse(content="ok", stop_reason=StopReason.END_TURN)])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    session = build_agent_session(s, record, interactive=False, user_home=user_home)
    run_turn(session, "hi")

    reloaded = s.ctx.session_repo.get(record.id)
    assert reloaded.kind == "CHAT"
    assert session.promoted_root is None
