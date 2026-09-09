"""Engine Protocol slice 5a: PLAN/BUILD/REVIEW as engine-level mode semantics."""

from __future__ import annotations

import json
import time

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.application.session_service import profile_for_mode, profile_for_session
from rinari.engine_protocol import turns as turns_module
from rinari.engine_protocol.server import EngineServer
from rinari.policy.engine import PermissionProfile


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
        _req("m-c", "session.create", {"cwd": str(tmp_path), "chat": True})
    )
    assert response is not None and response["ok"] is True
    return response["result"]["session"]["id"]


def test_profile_for_mode_maps_plan_review_to_read_only() -> None:
    assert profile_for_mode("plan") is PermissionProfile.READ_ONLY
    assert profile_for_mode("review") is PermissionProfile.READ_ONLY
    assert profile_for_mode("build") is PermissionProfile.WORKSPACE
    # Legacy/unknown values keep prior behavior (workspace default).
    assert profile_for_mode("ask") is PermissionProfile.WORKSPACE
    assert profile_for_mode(None) is PermissionProfile.WORKSPACE


def test_mode_set_roundtrip_and_event(server, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    response = server.handle_line(
        _req("m1", "session.mode.set", {"ref": session_id, "mode": "PLAN"})
    )
    assert response is not None and response["ok"] is True
    assert response["result"]["session"]["mode"] == "plan"
    events = server.drain_events()
    assert {"session.mode.changed"} == {e["event"] for e in events}
    payload = events[0]["payload"]
    assert payload["session_id"] == session_id
    assert payload["mode"] == "plan"


def test_mode_set_rejects_unknown_mode(server, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    response = server.handle_line(
        _req("m2", "session.mode.set", {"ref": session_id, "mode": "yolo"})
    )
    assert response is not None and response["ok"] is False
    assert response["error"]["code"] == "INVALID_USAGE"


def test_mode_set_unknown_session_is_not_found(server) -> None:
    response = server.handle_line(
        _req("m3", "session.mode.set", {"ref": "ses-nope", "mode": "plan"})
    )
    assert response is not None and response["ok"] is False
    assert response["error"]["code"] == "NOT_FOUND"


def test_plan_turn_builds_read_only_session(server, tmp_path, monkeypatch) -> None:
    session_id = _create_chat(server, tmp_path)
    ok = server.handle_line(_req("m4", "session.mode.set", {"ref": session_id, "mode": "review"}))
    assert ok is not None and ok["ok"] is True

    seen: dict = {}

    def spy(services, record, **kwargs):
        seen["profile"] = kwargs.get("profile")
        raise RuntimeError("stop before model")

    monkeypatch.setattr(turns_module, "build_agent_session", spy)
    started = server.handle_line(
        _req("m5", "session.turn.start", {"session_id": session_id, "message": "hi"})
    )
    # Runtime preparation is asynchronous: acceptance must not wait for it,
    # and preparation failures arrive through the terminal event stream.
    assert started is not None and started["ok"] is True
    deadline = time.monotonic() + 2
    failed = None
    while time.monotonic() < deadline and failed is None:
        failed = next(
            (event for event in server.drain_events() if event["event"] == "turn.failed"),
            None,
        )
        if failed is None:
            time.sleep(0.01)
    assert failed is not None
    assert failed["payload"]["error"]["code"] == "ENGINE_ERROR"
    assert seen.get("profile") is PermissionProfile.READ_ONLY


def test_desktop_session_defaults_and_permission_are_persisted(server, tmp_path) -> None:
    response = server.handle_line(
        _req(
            "p1",
            "session.create",
            {
                "cwd": str(tmp_path),
                "chat": True,
                "mode": "build",
                "permission_profile": "full-access",
            },
        )
    )
    assert response is not None and response["ok"] is True
    session = response["result"]["session"]
    assert session["mode"] == "build"
    assert session["permission_profile"] == "full-access"
    record = server._services.sessions.show(session["id"])
    assert profile_for_session(record) is PermissionProfile.FULL_ACCESS

    plan = server.handle_line(
        _req("p2", "session.mode.set", {"ref": session["id"], "mode": "plan"})
    )
    assert plan is not None and plan["ok"] is True
    record = server._services.sessions.show(session["id"])
    assert profile_for_session(record) is PermissionProfile.READ_ONLY
    assert record.permission_profile == "full-access"


def test_session_model_selection_is_persisted_per_chat(server, services, tmp_path) -> None:
    provider = services.providers.add(
        AddProviderInput(
            alias="xainner",
            provider_type="custom",
            endpoint="https://example.invalid/v1",
            secret="dummy-secret-not-real",
        )
    )
    model = services.models.add("xainner", "uncensored", "uncensored")
    session_id = _create_chat(server, tmp_path)

    response = server.handle_line(
        _req(
            "model-session",
            "session.model.set",
            {"ref": session_id, "model": model.id, "provider": provider.id},
        )
    )

    assert response is not None and response["ok"] is True
    assert response["result"]["session"]["provider_id"] == provider.id
    assert response["result"]["session"]["model_id"] == model.id
    persisted = services.sessions.show(session_id)
    assert persisted.provider_id == provider.id
    assert persisted.model_id == model.id
    event_payload = server.drain_events()[-1]["payload"]
    assert event_payload["session_id"] == session_id
    assert event_payload["model_id"] == model.id
