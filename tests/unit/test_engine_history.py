"""Engine Protocol slice 4a: session.history over persisted conversation rows."""

from __future__ import annotations

import json
import time

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.engine_protocol.server import EngineServer
from rinari.models.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
)
from rinari.storage.records import SessionMessageRecord


class _AnswerModel:
    """Minimal scripted caller for the live-persistence test."""

    def __init__(self, scripted: list[ModelResponse]) -> None:
        self.scripted = scripted

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        return self.scripted.pop(0)


def _collect_until(server, session_id, timeout=30.0):
    terminal = {"turn.completed", "turn.cancelled", "turn.failed"}
    seen: list = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in server.drain_events():
            if evt.get("payload", {}).get("session_id") != session_id:
                continue
            seen.append(evt)
            if evt.get("event") in terminal:
                return seen
        time.sleep(0.02)
    return seen


@pytest.fixture
def services(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    container = build_services(app_ctx, user_home=user_home)
    container.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    container.models.add("fake", "fake-model-1", "fake-one")
    container.providers.use("fake")
    return container


@pytest.fixture
def server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


def _req(request_id, method, params=None):
    line: dict = {"id": request_id, "method": method}
    if params is not None:
        line["params"] = params
    return json.dumps(line)


def _create_chat(server, tmp_path):
    response = server.handle_line(
        _req("h-c", "session.create", {"cwd": str(tmp_path), "chat": True})
    )
    assert response is not None and response["ok"] is True
    return response["result"]["session"]["id"]


def _seed(services, session_id, roles):
    records = [
        SessionMessageRecord(
            id=f"msg-{index}",
            session_id=session_id,
            seq=0,
            role=role,
            content=f"text-{index}",
            tool_calls=[{"id": "tc-1", "name": "read", "arguments": "{}"}]
            if role == "assistant" and index == 1
            else None,
            created_at="2026-01-01T00:00:00Z",
        )
        for index, role in enumerate(roles)
    ]
    services.ctx.message_repo.append_many(session_id, records)


def test_history_empty_for_new_session(server, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    response = server.handle_line(_req("h1", "session.history", {"ref": session_id}))
    assert response is not None and response["ok"] is True
    result = response["result"]
    assert result["session_id"] == session_id
    assert result["messages"] == []
    assert result["total"] == 0
    assert result["has_more"] is False


def test_history_returns_rows_in_order(server, services, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    _seed(services, session_id, ["user", "assistant", "user"])
    response = server.handle_line(_req("h2", "session.history", {"ref": session_id}))
    assert response is not None and response["ok"] is True
    messages = response["result"]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert [m["content"] for m in messages] == ["text-0", "text-1", "text-2"]
    assert [m["seq"] for m in messages] == [1, 2, 3]
    assert messages[1]["tool_calls"] == [{"id": "tc-1", "name": "read", "arguments": "{}"}]
    assert messages[0]["tool_calls"] is None
    assert response["result"]["total"] == 3
    assert response["result"]["has_more"] is False


def test_history_limit_returns_tail_window(server, services, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    _seed(services, session_id, ["user"] * 5)
    response = server.handle_line(_req("h3", "session.history", {"ref": session_id, "limit": 2}))
    assert response is not None and response["ok"] is True
    result = response["result"]
    assert [m["seq"] for m in result["messages"]] == [4, 5]
    assert result["total"] == 5
    assert result["has_more"] is True


def test_history_unknown_session_is_not_found(server) -> None:
    response = server.handle_line(_req("h4", "session.history", {"ref": "ses-nope"}))
    assert response is not None and response["ok"] is False
    assert response["error"]["code"] == "NOT_FOUND"


def test_history_rejects_bad_limit(server, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    response = server.handle_line(_req("h5", "session.history", {"ref": session_id, "limit": 0}))
    assert response is not None and response["ok"] is False
    assert response["error"]["code"] == "INVALID_PARAMS"


def test_protocol_turn_persists_messages(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    monkeypatch.setattr(
        agent_runtime,
        "_caller_for",
        lambda services, rec: _AnswerModel(
            scripted=[ModelResponse(content="hola", stop_reason=StopReason.END_TURN)]
        ),
    )
    started = server.handle_line(
        _req("h6", "session.turn.start", {"session_id": session_id, "message": "hi"})
    )
    assert started is not None and started["ok"] is True
    events = _collect_until(server, session_id)
    assert events and events[-1]["event"] == "turn.completed"

    deadline = time.time() + 10
    history = None
    while time.time() < deadline:
        response = server.handle_line(_req("h7", "session.history", {"ref": session_id}))
        assert response is not None and response["ok"] is True
        if response["result"]["total"] >= 2:
            history = response["result"]["messages"]
            break
        time.sleep(0.05)
    assert history is not None, "protocol turn must persist user + assistant rows"
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "hi"
    assert history[-1]["role"] == "assistant"
    assert "hola" in (history[-1]["content"] or "")
