"""Engine Protocol slice 11: prompt queue, profile bundles, `rinari code`."""

from __future__ import annotations

import json
import time

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.cli.commands.code import _resolve_binary
from rinari.engine_protocol.server import EngineServer
from rinari.models.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
)
from rinari.shared.errors import InvalidUsageError


class FakeModel:
    def __init__(self, scripted):
        self.scripted = scripted

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        return self.scripted.pop(0)


def _answer(text="hola") -> ModelResponse:
    return ModelResponse(content=text, stop_reason=StopReason.END_TURN)


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


def _create_chat(server, tmp_path, tag="t"):
    created = _ok(server.handle_line(_req(f"{tag}-c", "session.create", {"cwd": str(tmp_path)})))
    return created["session"]["id"]


def _wait_completed(server, session_id, count, timeout=30.0):
    seen = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in server.drain_events():
            if (
                evt.get("event") == "turn.completed"
                and evt.get("payload", {}).get("session_id") == session_id
            ):
                seen.append(evt)
                if len(seen) >= count:
                    return seen
        time.sleep(0.02)
    return seen


def test_queue_add_list_clear_validation(server, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path, "q")
    added = _ok(
        server.handle_line(
            _req("q1", "session.queue.add", {"session_id": session_id, "message": "uno"})
        )
    )
    assert added["position"] == 1
    _ok(
        server.handle_line(
            _req("q2", "session.queue.add", {"session_id": session_id, "message": "dos"})
        )
    )
    listed = _ok(server.handle_line(_req("q3", "session.queue.list", {"session_id": session_id})))
    assert listed["queue"] == ["uno", "dos"]
    assert (
        _err(
            server.handle_line(
                _req("q4", "session.queue.add", {"session_id": session_id, "message": "  "})
            )
        )["code"]
        == "INVALID_PARAMS"
    )
    cleared = _ok(server.handle_line(_req("q5", "session.queue.clear", {"session_id": session_id})))
    assert cleared["removed"] == 2
    assert (
        _ok(server.handle_line(_req("q6", "session.queue.list", {"session_id": session_id})))[
            "pending"
        ]
        == 0
    )


def test_queue_runs_after_live_turn(server, tmp_path, monkeypatch) -> None:
    shared = FakeModel(scripted=[_answer("primero"), _answer("segundo")])
    monkeypatch.setattr(
        agent_runtime,
        "_caller_for",
        lambda services, rec: shared,
    )
    session_id = _create_chat(server, tmp_path, "qr")
    _ok(
        server.handle_line(
            _req("qr-s", "session.turn.start", {"session_id": session_id, "message": "hola"})
        )
    )
    _ok(
        server.handle_line(
            _req("qr-q", "session.queue.add", {"session_id": session_id, "message": "sigue"})
        )
    )
    done = _wait_completed(server, session_id, 2)
    assert len(done) == 2
    assert [e["payload"]["content"] for e in done] == ["primero", "segundo"]


def test_profile_bundle_crud_and_apply(server, tmp_path) -> None:
    assert _ok(server.handle_line(_req("b1", "profile_bundle.list", {})))["profiles"] == []
    created = _ok(
        server.handle_line(
            _req(
                "b2",
                "profile_bundle.create",
                {"id": "foco", "name": "Foco", "soul_id": "rinari-default", "mode": "plan"},
            )
        )
    )["profile"]
    assert created["soul_id"] == "rinari-default"
    assert created["mode"] == "plan"
    assert (
        _err(
            server.handle_line(_req("b3", "profile_bundle.create", {"id": "foco", "name": "Dup"}))
        )["code"]
        == "CONFLICT"
    )
    assert (
        _err(server.handle_line(_req("b4", "profile_bundle.create", {"id": "BAD", "name": "x"})))[
            "code"
        ]
        == "INVALID_USAGE"
    )
    session_id = _create_chat(server, tmp_path, "b")
    applied = _ok(
        server.handle_line(
            _req("b5", "profile_bundle.apply", {"id": "foco", "session_ref": session_id})
        )
    )["applied"]
    assert applied["soul_id"] == "rinari-default"
    assert applied["mode"] == "plan"
    assert applied["session_id"] == session_id
    listed = _ok(server.handle_line(_req("b6", "profile_bundle.list", {})))["profiles"]
    assert [p["id"] for p in listed] == ["foco"]
    removed = _ok(server.handle_line(_req("b7", "profile_bundle.remove", {"id": "foco"})))
    assert removed["removed"] == {"id": "foco"}
    assert (
        _err(server.handle_line(_req("b8", "profile_bundle.get", {"id": "foco"})))["code"]
        == "NOT_FOUND"
    )


def test_code_binary_resolution() -> None:
    with pytest.raises(InvalidUsageError):
        _resolve_binary("/definitely/not/a/real/binary-xyz")
