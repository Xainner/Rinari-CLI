"""A turn left open by an engine that exited is closed, once, and never a live one."""

from __future__ import annotations

import json
import threading
import time

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.engine_protocol.orphans import ENGINE_EXITED, find_orphans
from rinari.engine_protocol.server import EngineServer
from rinari.models.types import ModelResponse, ProviderCapabilities, StopReason
from rinari.sessions.turn_lock import SessionTurnLock
from rinari.shared.clock import now_iso
from rinari.storage.records import SessionEventRecord


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


def _req(request_id, method, params=None):
    return json.dumps({"id": request_id, "method": method, "params": params or {}})


def _session(services, tmp_path) -> str:
    engine = EngineServer(services, user_home=tmp_path / "home")
    try:
        response = engine.handle_line(
            _req("c", "session.create", {"cwd": str(tmp_path), "chat": True})
        )
        return response["result"]["session"]["id"]
    finally:
        engine.close()


def _write(services, session_id, turn_id, kind, seq, **payload):
    ctx = services.ctx
    ctx.event_repo.insert(
        SessionEventRecord(
            id=ctx.ids.new("evt"),
            session_id=session_id,
            seq=ctx.event_repo.next_seq(session_id),
            type=kind,
            payload={
                "session_id": session_id,
                "turn_id": turn_id,
                "activity_seq": seq,
                "occurred_at": now_iso(ctx.clock),
                **payload,
            },
            created_at=now_iso(ctx.clock),
            turn_id=turn_id,
            activity_seq=seq,
        )
    )


def _dead_turn(services, session_id, turn_id="turn_dead"):
    """What a process killed inside a blocking tool leaves behind."""
    _write(services, session_id, turn_id, "turn.started", 1, message="espera al proceso")
    _write(services, session_id, turn_id, "model.started", 2, model_call_id="model_1")
    _write(services, session_id, turn_id, "model.completed", 2, model_call_id="model_1")
    _write(services, session_id, turn_id, "tool.requested", 3, tool_call_id="c1", tool="x")
    _write(services, session_id, turn_id, "tool.started", 3, tool_call_id="c1", tool="x")
    _write(services, session_id, turn_id, "agent.started", 4, agent_id="a1", agent="explorer")


def _events(services, session_id, turn_id):
    return [r for r in services.ctx.event_repo.list(session_id) if r.turn_id == turn_id]


def test_engine_start_closes_a_turn_the_previous_engine_left_open(services, tmp_path) -> None:
    session_id = _session(services, tmp_path)
    _dead_turn(services, session_id)

    engine = EngineServer(services, user_home=tmp_path / "home")
    try:
        rows = _events(services, session_id, "turn_dead")
        kinds = [r.type for r in rows]
        assert kinds[-1] == "turn.failed"
        failed = rows[-1].payload["error"]
        assert failed["code"] == ENGINE_EXITED
        assert failed["retryable"] is True
        assert failed["details"]["reason"] == "engine_exited"
        cancelled = next(r for r in rows if r.type == "tool.cancelled")
        assert cancelled.payload["tool_call_id"] == "c1"
        assert next(r for r in rows if r.type == "agent.failed").payload["agent_id"] == "a1"
        # The model call had finished: only open activities are closed.
        assert "model.failed" not in kinds
        # Closing rows come after everything the turn wrote, in order.
        seqs = [r.activity_seq for r in rows]
        assert seqs[-3:] == sorted(seqs[-3:]) and seqs[-1] > 4
        # A connected client learns it too.
        emitted = [e["event"] for e in engine.drain_events()]
        assert emitted[-1] == "turn.failed"
        timeline = engine.handle_line(_req("t", "session.timeline", {"ref": session_id}))
        assert timeline["result"]["turns"][-1]["status"] == "failed"
    finally:
        engine.close()


def test_reconciling_twice_writes_nothing_more(services, tmp_path) -> None:
    session_id = _session(services, tmp_path)
    _dead_turn(services, session_id)
    engine = EngineServer(services, user_home=tmp_path / "home")
    try:
        before = len(services.ctx.event_repo.list(session_id))
        assert engine._turns.reconcile_orphan_turns(session_id) == []
        engine.handle_line(_req("o", "session.open", {"ref": session_id}))
        engine.handle_line(_req("t", "session.timeline", {"ref": session_id}))
        assert len(services.ctx.event_repo.list(session_id)) == before
        assert find_orphans(services.ctx.db) == []
    finally:
        engine.close()


def test_a_session_whose_turn_lock_is_held_elsewhere_is_left_alone(services, tmp_path) -> None:
    session_id = _session(services, tmp_path)
    _dead_turn(services, session_id)
    lock = services.ctx.layout.dir("sessions") / f"{session_id}.turn.lock"
    with SessionTurnLock(lock, session_id):
        engine = EngineServer(services, user_home=tmp_path / "home")
        try:
            assert find_orphans(services.ctx.db, session_id) == [(session_id, "turn_dead")]
        finally:
            engine.close()
    engine = EngineServer(services, user_home=tmp_path / "home")
    try:
        assert find_orphans(services.ctx.db, session_id) == []
    finally:
        engine.close()


class _BlockingModel:
    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate

    def capabilities(self):
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request):
        self.gate.wait(10)
        return ModelResponse(content="hola", stop_reason=StopReason.END_TURN)


def test_a_turn_running_in_this_engine_is_never_closed(services, tmp_path, monkeypatch) -> None:
    session_id = _session(services, tmp_path)
    gate = threading.Event()
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda *_: _BlockingModel(gate))
    engine = EngineServer(services, user_home=tmp_path / "home")
    try:
        started = engine.handle_line(
            _req("s", "session.turn.start", {"session_id": session_id, "message": "hola"})
        )
        turn_id = started["result"]["turn_id"]
        assert engine._turns.reconcile_orphan_turns(session_id) == []
        engine.handle_line(_req("t", "session.timeline", {"ref": session_id}))
        assert "turn.failed" not in [r.type for r in _events(services, session_id, turn_id)]
        gate.set()
        deadline = time.time() + 10
        while engine.has_active_turns() and time.time() < deadline:
            time.sleep(0.02)
        kinds = [r.type for r in _events(services, session_id, turn_id)]
        assert kinds[-1] == "turn.completed" and "turn.failed" not in kinds
    finally:
        gate.set()
        engine.close()
