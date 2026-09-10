"""P0 contract pins for the desktop protocol (docs/desktop README + 02-C + 05-C).

No engine code is expected for these: they freeze what Rinari Code already
depends on after its stabilization cycle — history ordering/shape, events
pagination, snapshot shape, cancel terminality. If any pin must change,
bump protocol.PROTOCOL_VERSION instead of silently drifting.
"""

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
from rinari.models.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
)
from rinari.shared.errors import CancelledError
from rinari.storage.records import SessionMessageRecord

TERMINAL = {"turn.completed", "turn.cancelled", "turn.failed"}


@dataclass
class FakeModel:
    scripted: list[ModelResponse]
    requests: list[ModelRequest] = field(default_factory=list)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
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


def _answer() -> ModelResponse:
    return ModelResponse(content="hola", stop_reason=StopReason.END_TURN)


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
        _req("pin-c", "session.create", {"cwd": str(tmp_path), "chat": True})
    )
    assert response is not None and response["ok"] is True
    return response["result"]["session"]["id"]


def _collect_until(server, session_id, timeout=30.0):
    seen: list = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in server.drain_events():
            if evt.get("payload", {}).get("session_id") != session_id:
                continue
            seen.append(evt)
            if evt.get("event") in TERMINAL:
                return seen
        time.sleep(0.02)
    return seen


def _start(server, session_id, tag="t"):
    response = server.handle_line(
        _req(f"{tag}-start", "session.turn.start", {"session_id": session_id, "message": "hi"})
    )
    assert response is not None and response["ok"] is True
    return response["result"]["turn_id"]


def _seed(services, session_id):
    """Rows in final order: the store renumbers seqs on append (ORDER BY seq).

    The pin is that reads come back ascending with persisted timestamps,
    whatever the store assigned.
    """
    rows = [
        ("user", "2026-09-09T10:00:00Z", None, None, None),
        ("tool", "2026-09-09T10:00:01Z", None, "tc-1", "read"),
        (
            "assistant",
            "2026-09-09T10:00:02Z",
            [{"id": "tc-1", "name": "read", "arguments": "{}"}],
            None,
            None,
        ),
    ]
    records = [
        SessionMessageRecord(
            id=f"msg-{index}",
            session_id=session_id,
            seq=0,
            role=role,
            content=f"text-{role}",
            tool_calls=calls,
            tool_call_id=call_id,
            name=name,
            created_at=created_at,
        )
        for index, (role, created_at, calls, call_id, name) in enumerate(rows)
    ]
    services.ctx.message_repo.append_many(session_id, records)


# -- docs/desktop 02-C: history ordering + persisted timestamps -----------------


def test_history_rows_ascending_by_seq_with_persisted_timestamps(
    server, services, tmp_path
) -> None:
    session_id = _create_chat(server, tmp_path)
    _seed(services, session_id)
    response = server.handle_line(_req("h", "session.history", {"ref": session_id}))
    assert response is not None and response["ok"] is True
    rows = response["result"]["messages"]
    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    assert [r["role"] for r in rows] == ["user", "tool", "assistant"]
    assert [r["created_at"] for r in rows] == [
        "2026-09-09T10:00:00Z",
        "2026-09-09T10:00:01Z",
        "2026-09-09T10:00:02Z",
    ]


def test_history_row_shape_is_additive_and_tool_calls_stay_structured(
    server, services, tmp_path
) -> None:
    session_id = _create_chat(server, tmp_path)
    _seed(services, session_id)
    response = server.handle_line(_req("h", "session.history", {"ref": session_id}))
    rows = {r["role"]: r for r in response["result"]["messages"]}
    required = {"id", "seq", "role", "content", "tool_calls", "tool_call_id", "name", "created_at"}
    for row in rows.values():
        assert required <= set(row), f"row lost fields: {required - set(row)}"
    assistant = rows["assistant"]
    assert isinstance(assistant["tool_calls"], list)
    assert assistant["tool_calls"][0]["name"] == "read"
    assert "read" not in (assistant["content"] or "")
    tool_row = rows["tool"]
    assert tool_row["tool_call_id"] == "tc-1" and tool_row["name"] == "read"


# -- docs/desktop 02-C: session.events as activity source -------------------------


def test_events_paginate_with_after_seq_and_limit(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    monkeypatch.setattr(
        agent_runtime, "_caller_for", lambda services, rec: FakeModel(scripted=[_answer()])
    )
    _start(server, session_id)
    _collect_until(server, session_id)

    first = server.handle_line(
        _req("e1", "session.events", {"ref": session_id, "after_seq": 0, "limit": 1})
    )
    assert first is not None and first["ok"] is True
    assert len(first["result"]["events"]) == 1
    assert first["result"]["has_more"] is True
    head_seq = first["result"]["events"][0]["seq"]

    rest = server.handle_line(
        _req("e2", "session.events", {"ref": session_id, "after_seq": head_seq})
    )
    assert rest is not None and rest["ok"] is True
    seqs = [e["seq"] for e in rest["result"]["events"]]
    assert seqs == sorted(seqs) and all(s > head_seq for s in seqs)
    for evt in rest["result"]["events"]:
        assert {"id", "seq", "type", "payload", "created_at"} <= set(evt)
        assert isinstance(evt["type"], str) and evt["type"] and isinstance(evt["payload"], dict)

    # NOTE (docs/desktop 02-C gap): turn lifecycle terminals (turn.completed
    # and friends) are live-only today — turns.py never persists to
    # event_repo — so a completed turn is NOT expected back here. The live
    # terminality pins below cover delivery; persisting terminals for
    # historical panels is engine work, not a P0 pin.
    types = {e["type"] for e in rest["result"]["events"]}
    assert "turn.completed" in types

    bad = server.handle_line(_req("e3", "session.events", {"ref": session_id, "limit": 0}))
    assert bad is not None and bad["ok"] is False


# -- docs/desktop 05-C: snapshot shape ----------------------------------------------


def test_snapshot_shape_while_turn_runs(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    gate, abort = threading.Event(), threading.Event()
    monkeypatch.setattr(
        agent_runtime,
        "_caller_for",
        lambda services, rec: FakeBlockingModel(scripted=[_answer()], gate=gate, abort=abort),
    )
    turn_id = _start(server, session_id)
    try:
        deadline = time.time() + 10
        seen = False
        while time.time() < deadline:
            response = server.handle_line(_req("s", "runtime.snapshot.get", {}))
            snap = response["result"]["snapshot"]
            if any(t["session_id"] == session_id for t in snap["active_turns"]):
                seen = True
                break
            time.sleep(0.02)
        assert seen, "busy turn never appeared in the snapshot"

        assert isinstance(snap["protocol_version"], int) and snap["protocol_version"] >= 1
        assert isinstance(snap["sessions"], list) and isinstance(snap["providers"], list)
        assert isinstance(snap["pending_approvals"], list)
        mine = [t for t in snap["active_turns"] if t["session_id"] == session_id]
        assert len(mine) == 1
        turn = mine[0]
        assert turn["turn_id"] == turn_id
        assert isinstance(turn["status"], str)
        assert isinstance(turn["started_at"], float) and turn["started_at"] > 0
        assert isinstance(turn["activities"], list)
        assert turn["items"] == turn["activities"]
        assert isinstance(turn["next_activity_seq"], int)
        for activity in turn["activities"]:
            assert {"event", "turn_id", "session_id"} <= set(activity)
    finally:
        gate.set()
        _collect_until(server, session_id)


def test_snapshot_empty_after_terminal(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    monkeypatch.setattr(
        agent_runtime, "_caller_for", lambda services, rec: FakeModel(scripted=[_answer()])
    )
    _start(server, session_id)
    _collect_until(server, session_id)
    response = server.handle_line(_req("s", "runtime.snapshot.get", {}))
    snap = response["result"]["snapshot"]
    assert all(t["session_id"] != session_id for t in snap["active_turns"])


# -- docs/desktop 05-C: cancel terminality --------------------------------------------


def test_completed_turn_emits_exactly_one_terminal(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    monkeypatch.setattr(
        agent_runtime, "_caller_for", lambda services, rec: FakeModel(scripted=[_answer()])
    )
    turn_id = _start(server, session_id)
    seen = _collect_until(server, session_id)
    terminals = [
        e
        for e in seen
        if e.get("event") in TERMINAL and e.get("payload", {}).get("turn_id") == turn_id
    ]
    assert [e["event"] for e in terminals] == ["turn.completed"]


def test_cancelled_turn_has_no_activity_after_terminal(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    gate, abort = threading.Event(), threading.Event()
    monkeypatch.setattr(
        agent_runtime,
        "_caller_for",
        lambda services, rec: FakeBlockingModel(scripted=[_answer()], gate=gate, abort=abort),
    )
    turn_id = _start(server, session_id)
    deadline = time.time() + 10
    while not server.has_active_turns() and time.time() < deadline:
        time.sleep(0.02)
    assert server.has_active_turns()

    cancelled = server.handle_line(_req("stop", "session.turn.cancel", {"session_id": session_id}))
    assert cancelled is not None and cancelled["ok"] is True
    seen = _collect_until(server, session_id)
    abort.set()
    time.sleep(0.3)
    seen.extend(
        e for e in server.drain_events() if e.get("payload", {}).get("session_id") == session_id
    )
    terminal_idx = next(
        i
        for i, e in enumerate(seen)
        if e.get("event") in TERMINAL and e.get("payload", {}).get("turn_id") == turn_id
    )
    assert seen[terminal_idx]["event"] == "turn.cancelled"
    after = [e for e in seen[terminal_idx + 1 :] if e.get("payload", {}).get("turn_id") == turn_id]
    assert after == []
