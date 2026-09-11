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
from rinari.engine_protocol import turns as turns_module
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


def test_workspace_search_and_text_attachment_reach_model(server, tmp_path, monkeypatch) -> None:
    note = tmp_path / "hardware.txt"
    note.write_text("CPU: test-chip", encoding="utf-8")
    session_id = _create_chat(server, tmp_path, tag="att")
    search = server.handle_line(
        _req("att-s", "workspace.file.search", {"session_id": session_id, "query": "hardware"})
    )
    assert search is not None and search["ok"] is True
    assert search["result"]["files"][0]["name"] == "hardware.txt"

    fake = FakeModel(scripted=[_answer()])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    started = server.handle_line(
        _req(
            "att-t",
            "session.turn.start",
            {
                "session_id": session_id,
                "message": "inspect",
                "attachments": [
                    {"id": "a1", "path": str(note), "name": note.name, "source": "workspace"}
                ],
            },
        )
    )
    assert started is not None and started["ok"] is True
    _collect_until(server, session_id)
    assert fake.requests
    assert "CPU: test-chip" in "\n".join(m.content or "" for m in fake.requests[0].messages)


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


def test_channel_request_uses_normal_runtime_and_private_resolution(server, tmp_path, monkeypatch):
    from rinari.models.types import ToolCall
    session_id = _create_chat(server, tmp_path, tag="channel")
    fake = FakeModel(scripted=[
        ModelResponse(
            content="",
            tool_calls=(ToolCall(
                id="find", name="capability.search", arguments={"query": "channel reply"}
            ),),
            stop_reason=StopReason.TOOL_CALLS,
        ),
        ModelResponse(
            content="",
            tool_calls=(ToolCall(
                id="send", name="channel.reply", arguments={"text": "Respuesta sintética"}
            ),),
            stop_reason=StopReason.TOOL_CALLS,
        ),
        _answer(),
    ])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    started = server.handle_line(_req("start", "operation.start", {
        "operation_id": "channel-test", "session_id": session_id, "message": "Responde citando",
        "channel": {"binding_id": "channel-test", "capabilities": ["channel.reply"]}}))
    assert started["ok"], started
    requested = None
    deadline = time.time() + 8
    while time.time() < deadline:
        result = server.handle_line(_req(str(time.monotonic_ns()), "channel.pending"))
        assert result["ok"], result
        if result["result"]["requests"]:
            requested = result["result"]["requests"][0]
            break
        time.sleep(0.02)
    assert requested, [m.content for r in fake.requests for m in r.messages if m.role == "tool"]
    resolved = server.handle_line(_req("resolve", "channel.resolve", {
        "request_id": requested["request_id"], "binding_id": "channel-test",
        "result": {"ok": True, "data": {"state": "sent", "delivery_id": "synthetic"}}}))
    assert resolved["ok"]
    events = _collect_until(server, session_id)
    assert any(e["event"] == "turn.completed" for e in events), events


def test_image_protocol_preserves_reference_and_reaches_model(
    server, services, tmp_path, monkeypatch
):
    from PIL import Image
    path = tmp_path / "synthetic.png"
    Image.new("RGB", (24, 24), "blue").save(path)
    session_id = _create_chat(server, tmp_path, tag="vision")
    configured = server.handle_line(_req("vision-config", "model.vision.set", {
        "ref": services.sessions.show(session_id).model_id, "enabled": True}))
    assert configured["ok"], configured
    imported = server.handle_line(_req("image-import", "artifact.receive_image", {
        "session_id": session_id, "path": str(path)}))
    assert imported["ok"], imported
    attachment = imported["result"]["attachment"]

    class Visual(FakeModel):
        def capabilities(self):
            return ProviderCapabilities(streaming=False, tool_calls=True, vision=True)

    fake = Visual([_answer()])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda *_: fake)
    started = server.handle_line(_req("vision-start", "operation.start", {
        "operation_id": "vision-test", "session_id": session_id,
        "message": "Describe", "attachments": [attachment]}))
    assert started["ok"], started
    events = _collect_until(server, session_id)
    assert any(e["event"] == "turn.completed" for e in events)
    user = next(m for m in fake.requests[0].messages if m.images)
    assert user.images[0].uri == attachment["uri"]
    assert attachment["uri"] in user.content
    listed = server.handle_line(
        _req("image-list", "artifact.media_list", {"session_id": session_id})
    )
    assert listed["result"]["count"] == 1
    deleted = server.handle_line(
        _req("image-delete", "artifact.delete_media", {"uri": attachment["uri"]})
    )
    assert deleted["ok"], deleted


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
    delta_events = [e for e in events if e["event"] == "model.content.delta"]
    deltas = [e["payload"]["delta"] for e in delta_events]
    assert deltas == ["he", "llo"]
    assert {e["payload"]["model_call_id"] for e in delta_events} == {"model_1"}
    completed_content = next(e for e in events if e["event"] == "model.content.completed")
    assert completed_content["payload"]["model_call_id"] == "model_1"
    assert completed_content["payload"]["output_kind"] == "final"
    assert completed_content["payload"]["content"] == "hola"
    assert events[-1]["event"] == "turn.completed"


def test_persisted_timeline_replays_visible_model_content(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path, tag="timeline")
    monkeypatch.setattr(
        agent_runtime,
        "_caller_for",
        lambda services, rec: FakeStreamModel(scripted=[_answer()]),
    )
    started = server.handle_line(
        _req("timeline-start", "session.turn.start", {"session_id": session_id, "message": "hi"})
    )
    assert started is not None and started["ok"] is True
    turn_id = started["result"]["turn_id"]
    _collect_until(server, session_id)

    replay = server.handle_line(_req("timeline-read", "session.timeline", {"ref": session_id}))
    assert replay is not None and replay["ok"] is True
    turn = replay["result"]["turns"][0]
    assert turn["turn_id"] == turn_id
    assert turn["user_message"] == "hi"
    assert turn["status"] == "completed"
    assert turn["final_response"] == "hola"
    assert not any(item["event"] == "model.content.delta" for item in turn["items"])
    model = next(item for item in turn["items"] if item["event"] == "model.content.completed")
    assert model["model_call_id"] == "model_1"
    assert model["activity_seq"] > 0


def test_turn_forwards_reasoning_effort_to_model(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    fake = FakeModel(scripted=[_answer()])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)

    started = server.handle_line(
        _req(
            "t1",
            "session.turn.start",
            {"session_id": session_id, "message": "hi", "reasoning_effort": "high"},
        )
    )
    assert started is not None and started["ok"] is True

    events = _collect_until(server, session_id)
    assert fake.requests[0].reasoning_effort == "high"
    assert events[0]["payload"]["reasoning_effort"] == "high"


def test_turn_start_accepts_before_runtime_preparation_finishes(
    server, tmp_path, monkeypatch
) -> None:
    session_id = _create_chat(server, tmp_path)
    entered = threading.Event()
    release = threading.Event()
    real_build = turns_module.build_agent_session

    def delayed_build(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(turns_module, "build_agent_session", delayed_build)
    monkeypatch.setattr(
        agent_runtime, "_caller_for", lambda services, rec: FakeModel(scripted=[_answer()])
    )

    started_at = time.monotonic()
    started = server.handle_line(
        _req("t1", "session.turn.start", {"session_id": session_id, "message": "hi"})
    )
    elapsed = time.monotonic() - started_at

    assert started is not None and started["ok"] is True
    assert elapsed < 1
    assert entered.wait(1)
    release.set()
    assert _collect_until(server, session_id)[-1]["event"] == "turn.completed"


def test_runtime_preparation_timeout_becomes_terminal_failure(
    server, tmp_path, monkeypatch
) -> None:
    session_id = _create_chat(server, tmp_path, tag="prep-timeout")
    entered = threading.Event()
    release = threading.Event()
    real_build = turns_module.build_agent_session

    def blocked_build(*args, **kwargs):
        entered.set()
        release.wait(5)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(turns_module, "build_agent_session", blocked_build)
    monkeypatch.setattr(turns_module, "PREPARATION_TIMEOUT_S", 0.1)

    started = server.handle_line(
        _req("prep-timeout-t", "session.turn.start", {"session_id": session_id, "message": "hi"})
    )
    assert started is not None and started["ok"] is True
    assert entered.wait(1)
    events = _collect_until(server, session_id, timeout=1)
    release.set()

    assert events[-1]["event"] == "turn.failed"
    assert events[-1]["payload"]["error"]["code"] == "TURN_PREPARATION_TIMEOUT"


def test_cancel_interrupts_runtime_preparation(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path, tag="prep-cancel")
    entered = threading.Event()
    release = threading.Event()
    real_build = turns_module.build_agent_session

    def blocked_build(*args, **kwargs):
        entered.set()
        release.wait(5)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(turns_module, "build_agent_session", blocked_build)
    started = server.handle_line(
        _req("prep-cancel-t", "session.turn.start", {"session_id": session_id, "message": "hi"})
    )
    assert started is not None and started["ok"] is True
    assert entered.wait(1)

    cancelled = server.handle_line(
        _req("prep-cancel-stop", "session.turn.cancel", {"session_id": session_id})
    )
    assert cancelled is not None and cancelled["ok"] is True
    events = _collect_until(server, session_id, timeout=1)
    release.set()

    assert events[-1]["event"] == "turn.cancelled"


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

    terminal_started = time.monotonic()
    events = _collect_until(server, session_id)
    abort.set()
    assert time.monotonic() - terminal_started < 1
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
    invalid_effort = server.handle_line(
        _req(
            "t3",
            "session.turn.start",
            {"session_id": "ses_missing", "message": "hi", "reasoning_effort": "ultra"},
        )
    )
    assert invalid_effort is not None and invalid_effort["ok"] is False
    assert invalid_effort["error"]["code"] == "INVALID_PARAMS"


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
    repeated = server.handle_line(
        _req(
            "a3",
            "approval.resolve",
            {"approval_id": requested["approval_id"], "decision": "allow_once"},
        )
    )
    assert repeated is not None and repeated["ok"] is True
    assert repeated["result"]["status"] == "resolved"
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


def test_pending_approval_expires_cleanly_when_cancelled(server) -> None:
    manager = server.turns
    request = ApprovalRequest(
        capability="shell.exec",
        description="waiting command",
        session_id="ses_cancel",
    )
    token = CancellationToken()
    outcome: dict[str, object] = {}

    def wait_for_answer() -> None:
        try:
            manager._answer_approval(request, token)
        except Exception as exc:  # assertion inspects the worker result
            outcome["error"] = exc

    thread = threading.Thread(target=wait_for_answer, daemon=True)
    thread.start()
    requested = _wait_event(server, "ses_cancel", "approval.requested")["payload"]
    token.cancel()
    expired = _wait_event(server, "ses_cancel", "approval.expired")["payload"]
    thread.join(timeout=5)

    assert isinstance(outcome.get("error"), CancelledError)
    assert expired["approval_id"] == requested["approval_id"]
    assert expired["reason"] == "turn_cancelled"
    repeated = server.handle_line(
        _req(
            "a4",
            "approval.resolve",
            {"approval_id": requested["approval_id"], "decision": "deny"},
        )
    )
    assert repeated is not None and repeated["ok"] is True
    assert repeated["result"]["status"] == "expired"


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


def test_durable_operation_replay_and_restart(server, services, tmp_path, monkeypatch):
    fake = FakeModel([ModelResponse(content="done", stop_reason=StopReason.END_TURN)])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda *_a, **_kw: fake)
    session_id = _create_chat(server, tmp_path, tag="durable")
    params = {"operation_id": "durable-test", "session_id": session_id, "message": "hello"}
    first = server.handle_line(_req("op1", "operation.start", params))
    assert first["ok"], first
    second = server.handle_line(_req("op2", "operation.start", params))
    assert second["result"]["turn_id"] == first["result"]["turn_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = server._turns.operations.get("durable-test")
        if result["state"] == "completed":
            break
        time.sleep(0.02)
    assert result["state"] == "completed", result
    assert len(fake.requests) == 1
    conflict = server.handle_line(_req("op3", "operation.start", {**params, "message": "changed"}))
    assert not conflict["ok"]
    from rinari.engine_protocol.operations import OperationStore

    restarted = OperationStore(services.ctx.layout.root)
    assert restarted.get("durable-test")["state"] == "completed"
    restarted.claim("crash-test", session_id, "private message", None, "never-started")
    after_crash = OperationStore(services.ctx.layout.root)
    assert after_crash.get("crash-test")["state"] == "uncertain"
    replay, claimed = after_crash.claim("crash-test", session_id, "private message", None, "new")
    assert not claimed and replay["turn_id"] == "never-started"
    assert b"private message" not in after_crash.path.read_bytes()


def test_operation_cancellation_is_persisted(server, tmp_path, monkeypatch):
    gate, abort = threading.Event(), threading.Event()
    fake = FakeBlockingModel([], gate, abort)
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda *_a, **_kw: fake)
    session_id = _create_chat(server, tmp_path, tag="op-cancel")
    started = server.handle_line(
        _req(
            "oc1",
            "operation.start",
            {"operation_id": "cancel-operation", "session_id": session_id, "message": "hello"},
        )
    )
    assert started["ok"]
    try:
        deadline = time.monotonic() + 5
        while not fake.requests and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fake.requests
        result = server.handle_line(
            _req("oc2", "operation.cancel", {"operation_id": "cancel-operation"})
        )
        assert result["result"]["state"] == "cancelling"
        abort.set()
        while time.monotonic() < deadline:
            operation = server._turns.operations.get("cancel-operation")
            if operation["state"] == "cancelled":
                break
            time.sleep(0.01)
        assert operation["state"] == "cancelled"
    finally:
        abort.set()


def test_remote_operation_exposes_only_bound_ssh(server, services, tmp_path, monkeypatch):
    import base64

    from rinari.application.ssh_targets import TargetStore

    key = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32).decode()
    target = TargetStore(services.ctx.layout.root).add(
        {
            "id": "remote",
            "name": "Remote",
            "host": "192.0.2.10",
            "port": 22,
            "username": "test",
            "identity": "fixture",
            "host_key": "ssh-ed25519 " + key,
        }
    )
    session_id = _create_chat(server, tmp_path, tag="remote")
    fake = FakeModel([ModelResponse(content="done", stop_reason=StopReason.END_TURN)])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda *_a, **_kw: fake)
    session = agent_runtime.build_agent_session(
        services,
        services.sessions.show(session_id),
        interactive=False,
        user_home=tmp_path / "home",
        remote_target=target,
    )
    try:
        assert session.loop.tool_registry.names() == ["ssh.inspect"]
        schema = session.loop.tool_registry.get("ssh.inspect").input_schema
        assert schema["properties"]["target_id"]["enum"] == ["remote"]
    finally:
        session.end()
    params = {
        "operation_id": "remote-id",
        "session_id": session_id,
        "message": "inspect",
        "target_id": "remote",
        "target_revision": "wrong",
    }
    result = server.handle_line(_req("remote-wrong", "operation.start", params))
    assert not result["ok"]
    assert server._turns.operations.get("remote-id") is None
    channel_session = agent_runtime.build_agent_session(
        services, services.sessions.show(session_id), interactive=False,
        user_home=tmp_path / "home", remote_target=target,
        channel_host=lambda *_: None,
    )
    try:
        names = set(channel_session.loop.tool_registry.names())
        assert {"ssh.inspect", "artifact.import", "channel.send_attachment"} <= names
        assert not {"shell.exec", "fs.read", "fs.write", "process.start"} & names
    finally:
        channel_session.end()
