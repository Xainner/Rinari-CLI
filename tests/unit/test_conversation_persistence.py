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

    # The turn state is kept as a harness note (F42); the owner's turn is
    # the rest, in order.
    stored = [r for r in s.ctx.message_repo.list(record.id) if not _state(r)]
    assert [r.role for r in stored] == ["user", "assistant"]

    # New invocation (new process equivalent): history must come back.
    fake2 = FakeModel(scripted=[ModelResponse(content="again", stop_reason=StopReason.END_TURN)])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake2)
    session_b = build_agent_session(
        s, s.ctx.session_repo.get(record.id), interactive=False, user_home=user_home
    )
    history = [m for m in session_b.context.history if not _state(m)]
    assert [m.role for m in history] == ["user", "assistant"]
    assert history[0].content == "hello"
    assert history[1].content == "hi there"

    result_b = run_turn(session_b, "again")
    assert result_b.kind == "answer"
    last = fake2.requests[-1].messages
    contents = [m.content for m in last if m.content]
    assert "hello" in contents and "hi there" in contents and "again" in contents

    # Now 4 messages persisted in order, besides the state note.
    assert [r.role for r in s.ctx.message_repo.list(record.id) if not _state(r)] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def _state(message) -> bool:
    """A harness note the conversation keeps: its turn state or its local time."""
    return (message.origin or {}).get("source") in ("turn-state", "clock")


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
    assert [r.role for r in s.ctx.message_repo.list(record.id) if not _state(r)] == ["user"]
    assert s.ctx.session_repo.get(record.id).state == "interrupted"


def test_the_title_request_carries_the_session_id(env, monkeypatch) -> None:
    # Plan 0.4.0 #48: OpenCode Go refuses a request without x-opencode-session,
    # which comes from ModelRequest.session_id; the title call had none.
    tmp_path, user_home, s = env
    cwd = tmp_path / "work"
    cwd.mkdir()
    record = s.sessions.start(cwd, forced_chat=True).session
    fake = FakeModel(
        scripted=[
            ModelResponse(content="Diseño del juego", stop_reason=StopReason.END_TURN),
            ModelResponse(content="Va.", stop_reason=StopReason.END_TURN),
        ]
    )
    fake.model_id = "fake-model-1"  # the title call names the conversation's model
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    session = build_agent_session(s, record, interactive=False, user_home=user_home)
    run_turn(session, "Diseñemos un juego de naves espaciales")
    assert fake.requests, "no model call was made"
    assert all(request.session_id == record.id for request in fake.requests)
    assert s.sessions.show(record.id).title == "Diseño del juego"
