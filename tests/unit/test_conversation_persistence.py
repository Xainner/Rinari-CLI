"""Conversation persistence: history survives across invocations and a
cancelled turn keeps its partial exchange (harness.md section 28)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.cli.agent_runtime import build_agent_session, run_turn
from rinari.models.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
)
from rinari.shared.errors import CancelledError
from rinari.storage.records import SessionMessageRecord


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


def test_message_repository_roundtrip(app_ctx) -> None:
    now = "2026-08-17T00:00:00Z"
    recs = [
        SessionMessageRecord(id="m1", session_id="s1", seq=0, role="user", content="hello"),
        SessionMessageRecord(
            id="m2",
            session_id="s1",
            seq=0,
            role="assistant",
            content="",
            tool_calls=[{"id": "tc1", "name": "fs.read", "arguments": {"path": "a"}}],
        ),
        SessionMessageRecord(
            id="m3",
            session_id="s1",
            seq=0,
            role="tool",
            content="data",
            tool_call_id="tc1",
            name="fs.read",
            created_at=now,
        ),
    ]
    app_ctx.message_repo.append_many("s1", recs)
    listed = app_ctx.message_repo.list("s1")
    assert [r.seq for r in listed] == [1, 2, 3]
    assert [r.role for r in listed] == ["user", "assistant", "tool"]
    assert listed[1].tool_calls == [{"id": "tc1", "name": "fs.read", "arguments": {"path": "a"}}]
    assert listed[2].tool_call_id == "tc1"
    assert listed[2].created_at == now


def test_history_restored_across_invocations(env, monkeypatch) -> None:
    tmp_path, user_home, s = env
    cwd = tmp_path / "work"
    cwd.mkdir()
    record = s.sessions.start(cwd, forced_chat=True).session

    fake1 = FakeModel(scripted=[ModelResponse(content="hi there", stop_reason=StopReason.END_TURN)])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake1)
    session_a = build_agent_session(s, record, interactive=False, user_home=user_home)
    result_a = run_turn(session_a, "hello")
    assert result_a.kind == "answer"

    stored = s.ctx.message_repo.list(record.id)
    assert [r.role for r in stored] == ["user", "assistant"]

    # New invocation (new process equivalent): history must come back.
    fake2 = FakeModel(scripted=[ModelResponse(content="again", stop_reason=StopReason.END_TURN)])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake2)
    session_b = build_agent_session(
        s, s.ctx.session_repo.get(record.id), interactive=False, user_home=user_home
    )
    history = session_b.context.history
    assert [m.role for m in history] == ["user", "assistant"]
    assert history[0].content == "hello"
    assert history[1].content == "hi there"

    result_b = run_turn(session_b, "again")
    assert result_b.kind == "answer"
    last = fake2.requests[-1].messages
    contents = [m.content for m in last if m.content]
    assert "hello" in contents and "hi there" in contents and "again" in contents

    # Now 4 messages persisted in order.
    assert [r.role for r in s.ctx.message_repo.list(record.id)] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_cancelled_turn_persists_partial_history(env, monkeypatch) -> None:
    tmp_path, user_home, s = env
    cwd = tmp_path / "work"
    cwd.mkdir()
    record = s.sessions.start(cwd, forced_chat=True).session

    class ExplodingModel(FakeModel):
        def invoke(self, request: ModelRequest) -> ModelResponse:
            raise CancelledError("user interrupted")

    monkeypatch.setattr(
        agent_runtime,
        "_caller_for",
        lambda services, rec: ExplodingModel(scripted=[]),
    )
    session = build_agent_session(s, record, interactive=False, user_home=user_home)
    session.token.cancel()
    result = run_turn(session, "interrupted prompt")
    assert result.kind == "cancelled"
    assert [r.role for r in s.ctx.message_repo.list(record.id)] == ["user"]
    assert s.ctx.session_repo.get(record.id).state == "interrupted"
