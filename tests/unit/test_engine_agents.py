"""Engine Protocol slice 7a: agent registry, per-agent model routing, session events."""

from __future__ import annotations

import json

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.engine_protocol.server import EngineServer
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
    container.models.add("fake", "fake-model-2", "fake-two")
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


def test_agent_list_has_builtins_with_empty_assignments(server) -> None:
    result = _ok(server.handle_line(_req("a1", "agent.list", {})))
    names = {a["name"] for a in result["agents"]}
    assert {"explore", "reviewer", "debugger", "researcher", "implementer", "verifier"} <= names
    explore = next(a for a in result["agents"] if a["name"] == "explore")
    assert explore["profile"] == "read-only"
    assert explore["assignment"] == {"model": None, "fallback": None, "enabled": True}


def test_agent_config_set_get_and_clear(server) -> None:
    result = _ok(
        server.handle_line(
            _req(
                "a2",
                "agent.config.set",
                {"agent": "explore", "model": "fake-one", "fallback": "fake-two"},
            )
        )
    )
    assert result["agent"]["assignment"] == {
        "model": "fake-one",
        "fallback": "fake-two",
        "enabled": True,
    }

    again = _ok(server.handle_line(_req("a3", "agent.config.get", {"agent": "explore"})))
    assert again["agent"]["assignment"]["model"] == "fake-one"

    cleared = _ok(
        server.handle_line(_req("a4", "agent.config.set", {"agent": "explore", "clear": True}))
    )
    assert cleared["agent"]["assignment"] == {"model": None, "fallback": None, "enabled": True}


def test_agent_config_rejects_unknown_agent_and_alias(server) -> None:
    error = _err(server.handle_line(_req("a5", "agent.config.get", {"agent": "nope"})))
    assert error["code"] == "NOT_FOUND"

    error = _err(
        server.handle_line(_req("a6", "agent.config.set", {"agent": "explore", "model": "ghost"}))
    )
    assert error["code"] == "NOT_FOUND"


def test_caller_for_agent_chain_and_inherit(services, tmp_path) -> None:
    record = services.sessions.new(cwd=tmp_path, title="t", forced_chat=True)
    # No assignment → inherit.
    assert agent_runtime.caller_for_agent(services, record, "explore") is None

    services.agent_configs.set("explore", model="fake-two")
    caller = agent_runtime.caller_for_agent(services, record, "explore")
    assert caller is not None
    assert caller.model_id != record.model_id

    # Stale primary degrades to fallback, then to parent.
    services.agent_configs.set("explore", model="ghost", fallback="fake-two")
    caller = agent_runtime.caller_for_agent(services, record, "explore")
    assert caller is not None
    services.agent_configs.set("explore", model="ghost", fallback="also-ghost")
    assert agent_runtime.caller_for_agent(services, record, "explore") is None

    # Disabled → inherit even with a valid model.
    services.agent_configs.set("explore", model="fake-two", enabled=False)
    assert agent_runtime.caller_for_agent(services, record, "explore") is None


def _create_chat(server, tmp_path):
    response = server.handle_line(
        _req("a-c", "session.create", {"cwd": str(tmp_path), "chat": True})
    )
    assert response is not None and response["ok"] is True
    return response["result"]["session"]["id"]


def test_session_events_empty_and_seeded(server, services, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    result = _ok(server.handle_line(_req("a7", "session.events", {"ref": session_id})))
    # Session creation itself persists a SessionStarted lifecycle event.
    assert [e["type"] for e in result["events"]] == ["SessionStarted"]

    services.ctx.event_repo.insert(
        SessionEventRecord(
            id="evt-1",
            session_id=session_id,
            seq=0,
            type="SubagentStart",
            payload={"agent": "explore", "state": "running"},
            created_at="2026-01-01T00:00:00Z",
        )
    )
    result = _ok(server.handle_line(_req("a8", "session.events", {"ref": session_id})))
    assert [e["type"] for e in result["events"]] == ["SessionStarted", "SubagentStart"]
    assert result["events"][1]["payload"]["agent"] == "explore"

    window = _ok(
        server.handle_line(_req("a9", "session.events", {"ref": session_id, "after_seq": 2}))
    )
    assert window["events"] == []


def test_session_events_unknown_session(server) -> None:
    error = _err(server.handle_line(_req("a10", "session.events", {"ref": "ses-nope"})))
    assert error["code"] == "NOT_FOUND"
