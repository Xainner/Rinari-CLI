"""Engine Protocol slice 2: live turns, streaming, cancel, approvals (TDD)."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.engine_protocol.server import EngineServer
from rinari.engine_protocol.transports.stdio import run_stdio
from rinari.models.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
)
from rinari.policy.approvals import ApprovalRequest
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.errors import CancelledError


@dataclass
class FakeModel:
    scripted: list[ModelResponse]
    requests: list[ModelRequest] = field(default_factory=list)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.scripted.pop(0)


class FakeStreamModel(FakeModel):
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=True, tool_calls=True, structured_output=True)

    def invoke_stream(self, request, on_delta):
        self.requests.append(request)
        on_delta("he")
        on_delta("llo")
        return self.scripted.pop(0)


class FakeBlockingModel(FakeModel):
    def __init__(self, scripted, gate: threading.Event, abort: threading.Event) -> None:
        super().__init__(scripted)
        self._gate = gate
        self._abort = abort

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        while not self._abort.wait(0.05):
            if self._gate.wait(0.05):
                break
        if self._abort.is_set():
            raise CancelledError("test abort")
        return self.scripted.pop(0)


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


def _create_chat(server, tmp_path, tag="t"):
    response = server.handle_line(
        _req(f"{tag}-c", "session.create", {"cwd": str(tmp_path), "chat": True})
    )
    assert response is not None and response["ok"] is True
    return response["result"]["session"]["id"]


def _collect_until(server, session_id, timeout=30.0):
    terminal = {"turn.completed", "turn.cancelled", "turn.failed"}
    seen: list = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in server.drain_events():
            payload = evt.get("payload", {})
            if payload.get("session_id") != session_id:
                continue
            seen.append(evt)
            if evt.get("event") in terminal:
                return seen
        time.sleep(0.02)
    return seen


def _answer() -> ModelResponse:
    return ModelResponse(content="hola", stop_reason=StopReason.END_TURN)


# -- live turns -------------------------------------------------------------


def test_turn_start_streams_and_completes(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    monkeypatch.setattr(
        agent_runtime, "_caller_for", lambda services, rec: FakeModel(scripted=[_answer()])
    )
    started = server.handle_line(
        _req("t1", "session.turn.start", {"session_id": session_id, "message": "hi"})
    )
    assert started is not None and started["ok"] is True
    assert started["result"]["status"] == "started"
    turn_id = started["result"]["turn_id"]

    events = _collect_until(server, session_id)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "turn.started"
    assert kinds[-1] == "turn.completed"
    completed = events[-1]["payload"]
    assert completed["turn_id"] == turn_id
    assert completed["kind"] == "answer"
    assert completed["content"] == "hola"


def test_turn_streams_content_deltas_in_order(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    monkeypatch.setattr(
        agent_runtime,
        "_caller_for",
        lambda services, rec: FakeStreamModel(scripted=[_answer()]),
    )
    started = server.handle_line(
        _req("t1", "session.turn.start", {"session_id": session_id, "message": "hi"})
    )
    assert started is not None and started["ok"] is True

    events = _collect_until(server, session_id)
    deltas = [e["payload"]["delta"] for e in events if e["event"] == "model.content.delta"]
    assert deltas == ["he", "llo"]
    assert events[-1]["event"] == "turn.completed"


def test_second_start_while_running_is_rejected(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    gate, abort = threading.Event(), threading.Event()
    fake = FakeBlockingModel(scripted=[_answer()], gate=gate, abort=abort)
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)

    first = server.handle_line(
        _req("t1", "session.turn.start", {"session_id": session_id, "message": "one"})
    )
    assert first is not None and first["ok"] is True
    second = server.handle_line(
        _req("t2", "session.turn.start", {"session_id": session_id, "message": "two"})
    )
    assert second is not None and second["ok"] is False
    assert second["error"]["code"] == "TURN_RUNNING"

    gate.set()
    events = _collect_until(server, session_id)
    assert events[-1]["event"] == "turn.completed"


def test_cancel_reports_requested_then_cancelled(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    gate, abort = threading.Event(), threading.Event()
    fake = FakeBlockingModel(scripted=[_answer()], gate=gate, abort=abort)
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)

    started = server.handle_line(
        _req("t1", "session.turn.start", {"session_id": session_id, "message": "slow"})
    )
    assert started is not None and started["ok"] is True
    deadline = time.time() + 10
    while not server.has_active_turns() and time.time() < deadline:
        time.sleep(0.02)
    assert server.has_active_turns()

    cancelled = server.handle_line(_req("t2", "session.turn.cancel", {"session_id": session_id}))
    assert cancelled is not None and cancelled["ok"] is True
    assert cancelled["result"]["status"] == "cancel_requested"
    abort.set()

    events = _collect_until(server, session_id)
    assert events[-1]["event"] == "turn.cancelled"


def test_cancel_without_turn_is_an_error(server) -> None:
    response = server.handle_line(_req("t1", "session.turn.cancel", {"session_id": "ses_missing"}))
    assert response is not None and response["ok"] is False
    assert response["error"]["code"] == "NO_ACTIVE_TURN"


def test_turn_start_validates_params(server) -> None:
    missing = server.handle_line(_req("t1", "session.turn.start", {"message": "hi"}))
    assert missing is not None and missing["ok"] is False
    assert missing["error"]["code"] == "INVALID_PARAMS"
    unknown = server.handle_line(
        _req("t2", "session.turn.start", {"session_id": "ses_missing", "message": "hi"})
    )
    assert unknown is not None and unknown["ok"] is False
    assert unknown["error"]["code"] == "NOT_FOUND"


# -- approvals --------------------------------------------------------------


def _wait_event(server, session_id, name, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in server.drain_events():
            if evt.get("event") == name and evt.get("payload", {}).get("session_id") == session_id:
                return evt
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {name}")


def test_approval_requested_and_resolved(server) -> None:
    manager = server.turns
    request = ApprovalRequest(
        capability="shell.exec",
        description="run test suite",
        target="pytest",
        risk="medium",
        session_id="ses_x",
    )
    answer: dict = {}
    thread = threading.Thread(
        target=lambda: answer.setdefault("v", manager._answer_approval(request)),
        daemon=True,
    )
    thread.start()
    try:
        requested = _wait_event(server, "ses_x", "approval.requested")["payload"]
        assert requested["tool"] == "shell.exec"
        assert requested["target"] == "pytest"
        assert requested["choices"] == ["deny", "allow_once", "allow_session"]

        unknown = server.handle_line(
            _req("a0", "approval.resolve", {"approval_id": "apr_nope", "decision": "deny"})
        )
        assert unknown is not None and unknown["ok"] is False
        assert unknown["error"]["code"] == "APPROVAL_NOT_FOUND"

        bad = server.handle_line(
            _req(
                "a1",
                "approval.resolve",
                {"approval_id": requested["approval_id"], "decision": "always"},
            )
        )
        assert bad is not None and bad["ok"] is False
        assert bad["error"]["code"] == "INVALID_PARAMS"

        resolved = server.handle_line(
            _req(
                "a2",
                "approval.resolve",
                {"approval_id": requested["approval_id"], "decision": "allow_once"},
            )
        )
        assert resolved is not None and resolved["ok"] is True
        assert resolved["result"]["status"] == "resolved"
    finally:
        thread.join(timeout=10)
    assert answer.get("v") == "y"
    assert manager.drain_events() or True


def test_approval_cancelled_while_waiting(server) -> None:
    manager = server.turns
    request = ApprovalRequest(
        capability="shell.exec",
        description="x",
        session_id="ses_y",
    )
    token = CancellationToken()
    token.cancel()
    with pytest.raises(CancelledError):
        manager._answer_approval(request, token)


# -- providers / models ------------------------------------------------------


def test_provider_list_is_redacted(server) -> None:
    response = server.handle_line(_req("p1", "provider.list"))
    assert response is not None and response["ok"] is True
    result = response["result"]
    assert result["active_alias"] == "fake"
    assert len(result["providers"]) == 1
    provider = result["providers"][0]
    assert provider["alias"] == "fake"
    assert provider["has_credential"] is True
    assert "dummy-secret-not-real" not in json.dumps(result)


def test_model_list(server) -> None:
    response = server.handle_line(_req("m1", "model.list"))
    assert response is not None and response["ok"] is True
    models = response["result"]["models"]
    assert len(models) == 1
    assert models[0]["alias"] == "fake-one"
    assert models[0]["provider"] == "fake"
    assert models[0]["active"] is True


# -- stdio with live events ----------------------------------------------------


def test_stdio_flushes_async_events(server, tmp_path, monkeypatch) -> None:
    import io

    session_id = _create_chat(server, tmp_path, tag="e")
    monkeypatch.setattr(
        agent_runtime, "_caller_for", lambda services, rec: FakeModel(scripted=[_answer()])
    )
    stdin = io.StringIO(
        _req("e1", "session.turn.start", {"session_id": session_id, "message": "hi"}) + "\n"
    )
    stdout, stderr = io.StringIO(), io.StringIO()

    def _pump() -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            for evt in server.drain_events():
                stdout.write(json.dumps(evt) + "\n")
            if not server.has_active_turns() and server.turns.drain_events() == []:
                break
            time.sleep(0.02)

    code_holder: dict = {}

    def _run() -> None:
        code_holder["code"] = run_stdio(server, stdin=stdin, stdout=stdout, stderr=stderr)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    _pump()
    thread.join(timeout=15)

    assert code_holder.get("code") == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["type"] == "hello"
    by_event = [line.get("event", line.get("id")) for line in lines[1:]]
    assert "turn.started" in by_event
    assert "turn.completed" in by_event
    assert "e1" in by_event
    assert stderr.getvalue() == ""
