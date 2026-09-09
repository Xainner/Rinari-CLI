"""Engine session lifecycle: close/delete + cascade (docs/desktop 02-A, P1).

Deviations from the proposal, pinned here on purpose:
- Tasks are project-scoped (shared across sessions) and are never deleted
  with a session; checkpoints and session-retention artifacts are kept
  unless cascade is true; the live turn queue is always drained.
- `cascade` defaults to false; close/delete on a running turn fail with
  TURN_RUNNING (no silent kill); closed sessions reject new turns with
  SESSION_CLOSED and disappear from default lists.
"""

from __future__ import annotations

import json
import threading
import time
from itertools import count

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
from rinari.shared.errors import CancelledError, InvalidUsageError, NotFoundError
from rinari.storage.records import SessionMessageRecord


class FakeModel:
    def __init__(self, scripted):
        self.scripted = scripted
        self.requests = []

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


def _ok(response):
    assert response is not None and response["ok"] is True, response
    return response["result"]


def _err(response):
    assert response is not None and response["ok"] is False, response
    return response["error"]


def _create_chat(server, tmp_path):
    response = server.handle_line(
        _req("lc-c", "session.create", {"cwd": str(tmp_path), "chat": True})
    )
    assert response is not None and response["ok"] is True
    return response["result"]["session"]["id"]


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


def _list_ids(server, **params):
    _list_ids.counter = getattr(_list_ids, "counter", count())
    rid = f"lst-{next(_list_ids.counter)}"
    return [s["id"] for s in _ok(server.handle_line(_req(rid, "session.list", params)))["sessions"]]


def _seed_checkpoint(services, session_id, tmp_path, tag="chk-1"):
    return services.checkpoints.repo.insert(
        {
            "id": tag,
            "session_ref": session_id,
            "project_root": str(tmp_path),
            "label": "pre",
            "agent_changes": 1,
            "user_owned": 0,
            "created_at": "2026-09-09T10:00:00Z",
        }
    )


def _blocked_turn(server, session_id, monkeypatch):
    gate, abort = threading.Event(), threading.Event()
    monkeypatch.setattr(
        agent_runtime,
        "_caller_for",
        lambda services, rec: FakeBlockingModel(scripted=[_answer()], gate=gate, abort=abort),
    )
    started = server.handle_line(
        _req("blk", "session.turn.start", {"session_id": session_id, "message": "slow"})
    )
    assert started is not None and started["ok"] is True
    deadline = time.time() + 10
    while not server.has_active_turns() and time.time() < deadline:
        time.sleep(0.02)
    assert server.has_active_turns()
    return gate, abort


def test_close_hides_from_default_list_and_resume_restores(server, services, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    closed = _ok(server.handle_line(_req("c1", "session.close", {"ref": session_id})))["session"]
    assert closed["state"] == "closed"
    assert session_id not in _list_ids(server)
    assert session_id in _list_ids(server, include_closed=True)
    # Idempotent: closing twice is fine.
    again = _ok(server.handle_line(_req("c2", "session.close", {"ref": session_id})))["session"]
    assert again["state"] == "closed"
    # Restore path: resume re-activates (existing service behavior).
    resumed = services.sessions.resume(session_id)
    assert resumed.session.state == "active"
    assert session_id in _list_ids(server)


def test_close_with_running_turn_is_turn_running(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    gate, _abort = _blocked_turn(server, session_id, monkeypatch)
    try:
        err = _err(server.handle_line(_req("c", "session.close", {"ref": session_id})))
        assert err["code"] == "TURN_RUNNING"
        assert session_id in _list_ids(server, include_closed=True)
    finally:
        gate.set()
        _collect_until(server, session_id)
    closed = _ok(server.handle_line(_req("c2", "session.close", {"ref": session_id})))["session"]
    assert closed["state"] == "closed"


def test_turn_start_on_closed_is_session_closed(server, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    _ok(server.handle_line(_req("c", "session.close", {"ref": session_id})))
    err = _err(
        server.handle_line(
            _req("t", "session.turn.start", {"session_id": session_id, "message": "hi"})
        )
    )
    assert err["code"] == "SESSION_CLOSED"


def test_delete_removes_record_and_drains_queue_but_keeps_owned_data(
    services, server, tmp_path
) -> None:
    session_id = _create_chat(server, tmp_path)
    _ok(
        server.handle_line(
            _req("q1", "session.queue.add", {"session_id": session_id, "message": "one"})
        )
    )
    _ok(
        server.handle_line(
            _req("q2", "session.queue.add", {"session_id": session_id, "message": "two"})
        )
    )
    artifact = services.artifacts.create_text(session_id, "notes", "plan.md", "hello")
    checkpoint = _seed_checkpoint(services, session_id, tmp_path)

    result = _ok(server.handle_line(_req("d", "session.delete", {"ref": session_id})))
    assert result["deleted"] == {"id": session_id}
    assert result["cascade"]["queue_dropped"] == 2
    assert result["cascade"]["checkpoints_kept"] == 1
    assert result["cascade"]["checkpoints_removed"] == 0
    assert result["cascade"]["artifacts_kept"] == 1
    assert result["cascade"]["artifacts_removed"] == 0

    assert _err(server.handle_line(_req("g", "session.get", {"ref": session_id})))["code"] == (
        "NOT_FOUND"
    )
    assert (
        _err(server.handle_line(_req("h", "session.history", {"ref": session_id})))["code"]
        == "NOT_FOUND"
    )
    # Kept data is still addressable.
    assert services.artifacts.get(artifact.uri()) == b"hello"
    assert services.checkpoints.show(checkpoint["id"])["id"] == checkpoint["id"]


def test_delete_cascade_removes_checkpoints_and_session_artifacts(
    services, server, tmp_path
) -> None:
    session_id = _create_chat(server, tmp_path)
    artifact = services.artifacts.create_text(session_id, "notes", "plan.md", "hello")
    checkpoint = _seed_checkpoint(services, session_id, tmp_path)

    result = _ok(
        server.handle_line(_req("d", "session.delete", {"ref": session_id, "cascade": True}))
    )
    assert result["cascade"]["checkpoints_removed"] == 1
    assert result["cascade"]["checkpoints_kept"] == 0
    assert result["cascade"]["artifacts_removed"] == 1
    assert result["cascade"]["artifacts_kept"] == 0

    with pytest.raises(InvalidUsageError):
        services.checkpoints.show(checkpoint["id"])
    with pytest.raises(NotFoundError):
        services.artifacts.get(artifact.uri())


def test_delete_with_running_turn_is_turn_running(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    gate, _abort = _blocked_turn(server, session_id, monkeypatch)
    try:
        err = _err(server.handle_line(_req("d", "session.delete", {"ref": session_id})))
        assert err["code"] == "TURN_RUNNING"
        assert session_id in _list_ids(server, include_closed=True)
    finally:
        gate.set()
        _collect_until(server, session_id)


def test_delete_unknown_session_is_not_found(server, tmp_path) -> None:
    _create_chat(server, tmp_path)
    err = _err(server.handle_line(_req("d", "session.delete", {"ref": "ses_missing"})))
    assert err["code"] == "NOT_FOUND"


def test_delete_rejects_non_boolean_cascade(server, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    err = _err(
        server.handle_line(_req("d", "session.delete", {"ref": session_id, "cascade": "yes"}))
    )
    assert err["code"] == "INVALID_PARAMS"
    assert session_id in _list_ids(server, include_closed=True)


def _seed_branch_source(services, session_id):
    services.ctx.message_repo.append_many(
        session_id,
        [
            SessionMessageRecord(
                id=f"bmsg-{i}",
                session_id=session_id,
                seq=0,
                role=role,
                content=text,
                created_at=f"2026-09-09T10:00:0{i}Z",
            )
            for i, (role, text) in enumerate([("user", "goal"), ("assistant", "plan")])
        ],
    )
    record = services.sessions.show(session_id)
    record.compact_state = {"goal": "goal"}
    services.ctx.session_repo.update(record)
    for tag, stamp in (("ck-old", "2026-09-09T10:00:00Z"), ("ck-new", "2026-09-09T10:00:01Z")):
        services.checkpoints.repo.insert(
            {
                "id": tag,
                "session_ref": session_id,
                "project_root": "root",
                "label": tag,
                "agent_changes": 0,
                "user_owned": 0,
                "created_at": stamp,
            }
        )


def test_branch_copies_conversation_compact_state_and_checkpoints(
    services, server, tmp_path
) -> None:
    session_id = _create_chat(server, tmp_path)
    _seed_branch_source(services, session_id)

    result = _ok(server.handle_line(_req("b", "session.branch", {"ref": session_id})))
    branch_id = result["session"]["id"]
    assert branch_id != session_id
    assert services.sessions.show(branch_id).forked_from == session_id
    assert result["branched_from"]["session_id"] == session_id
    assert result["branched_from"]["event_seq"] >= 1
    assert result["checkpoints_copied"] == 2

    history = _ok(server.handle_line(_req("h", "session.history", {"ref": branch_id})))["messages"]
    assert [(m["role"], m["content"]) for m in history] == [("user", "goal"), ("assistant", "plan")]
    branched = services.sessions.show(branch_id)
    assert branched.compact_state == {"goal": "goal"}
    labels = sorted(
        services.checkpoints.repo.get(cid)["label"]
        for cid in services.checkpoints.ids_for_session(branch_id)
    )
    assert labels == ["ck-new", "ck-old"]
    # Source untouched.
    assert len(services.checkpoints.ids_for_session(session_id)) == 2


def test_branch_with_checkpoint_id_copies_history_through_it(services, server, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    _seed_branch_source(services, session_id)

    result = _ok(
        server.handle_line(
            _req("b", "session.branch", {"ref": session_id, "checkpoint_id": "ck-old"})
        )
    )
    assert result["checkpoints_copied"] == 1
    branch_id = result["session"]["id"]
    labels = [
        services.checkpoints.repo.get(cid)["label"]
        for cid in services.checkpoints.ids_for_session(branch_id)
    ]
    assert labels == ["ck-old"]


def test_branch_unknown_checkpoint_rejected_without_branching(services, server, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    _seed_branch_source(services, session_id)
    before = set(_list_ids(server, include_closed=True))
    err = _err(
        server.handle_line(
            _req("b", "session.branch", {"ref": session_id, "checkpoint_id": "ck-nope"})
        )
    )
    assert err["code"] == "INVALID_PARAMS"
    assert set(_list_ids(server, include_closed=True)) == before


def test_branch_reopens_after_restart(services, server, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    _seed_branch_source(services, session_id)
    branch_id = _ok(server.handle_line(_req("b", "session.branch", {"ref": session_id})))[
        "session"
    ]["id"]
    server.close()
    fresh = EngineServer(services, user_home=tmp_path / "home")
    try:
        got = fresh.handle_line(_req("g", "session.get", {"ref": branch_id}))
        assert got is not None and got["ok"] is True
        history = fresh.handle_line(_req("h", "session.history", {"ref": branch_id}))
        assert history is not None and history["ok"] is True
        assert history["result"]["total"] == 2
    finally:
        fresh.close()
