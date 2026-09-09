"""Engine Protocol docs/desktop 04: Soul scopes (session override).

Session pin wins over the global Soul 3.0 chain; unknown ids never
persist; a pin pointing at a removed Soul fails loudly; subagents stay
functional (no Soul in their context).
"""

from __future__ import annotations

import json

import pytest

from rinari.agents.runtime import SubagentRuntimeConfig
from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli.agent_runtime import build_assembler_context
from rinari.engine_protocol.server import EngineServer
from rinari.soul.store import SoulStore

ALT_IDENTITY = (
    "You are Alt, a terse session-scoped test persona. "
    "Answer in short sentences and never mention you are a test fixture."
)


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


@pytest.fixture
def alt_soul(services):
    # Same home the engine and session service resolve Souls from
    # (services.ctx.home), not the EngineServer user_home.
    store = SoulStore(services.ctx.home)
    store.create("alt-soul", name="Alt", identity=ALT_IDENTITY)
    return "alt-soul"


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
    response = server.handle_line(_req("c", "session.create", {"cwd": str(tmp_path), "chat": True}))
    assert response is not None and response["ok"] is True
    return response["result"]["session"]["id"]


def test_effective_defaults_to_global_chain(services, server, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    result = _ok(server.handle_line(_req("e", "soul.get_effective", {"ref": session_id})))
    assert result["session_id"] == session_id
    # Fresh home: no activation, no legacy ~/soul.md → bundled default.
    assert result["source"] == "default"
    assert result["soul_id"] == "rinari-default"


def test_session_pin_wins_over_global(services, server, tmp_path, alt_soul) -> None:
    session_id = _create_chat(server, tmp_path)
    _ok(server.handle_line(_req("a", "soul.activate", {"id": "rinari-default"})))
    result = _ok(
        server.handle_line(_req("s", "session.soul.set", {"ref": session_id, "id": alt_soul}))
    )
    assert result["session"]["soul_id"] == alt_soul
    assert result["soul_id"] == alt_soul
    assert result["source"] == "session"

    effective = _ok(server.handle_line(_req("e", "soul.get_effective", {"ref": session_id})))
    assert (effective["soul_id"], effective["source"]) == (alt_soul, "session")

    # The override reaches prompt composition, not just the metadata.
    context = build_assembler_context(services, services.sessions.show(session_id))
    assert "terse session-scoped test persona" in context.soul


def test_set_unknown_soul_rejected_without_writing(server, tmp_path) -> None:
    session_id = _create_chat(server, tmp_path)
    err = _err(
        server.handle_line(_req("s", "session.soul.set", {"ref": session_id, "id": "ghost-soul"}))
    )
    assert err["code"] == "NOT_FOUND"
    session = _ok(server.handle_line(_req("g", "session.get", {"ref": session_id})))["session"]
    assert session["soul_id"] is None


def test_clear_restores_global(services, server, tmp_path, alt_soul) -> None:
    session_id = _create_chat(server, tmp_path)
    _ok(server.handle_line(_req("ss", "session.soul.set", {"ref": session_id, "id": alt_soul})))
    result = _ok(server.handle_line(_req("sc", "session.soul.clear", {"ref": session_id})))
    assert result["session"]["soul_id"] is None
    assert result["source"] == "default"
    assert result["soul_id"] == "rinari-default"


def test_pin_to_removed_soul_fails_loudly(server, tmp_path, alt_soul) -> None:
    session_id = _create_chat(server, tmp_path)
    _ok(server.handle_line(_req("s", "session.soul.set", {"ref": session_id, "id": alt_soul})))
    _ok(server.handle_line(_req("r", "soul.remove", {"id": alt_soul})))
    err = _err(server.handle_line(_req("e", "soul.get_effective", {"ref": session_id})))
    assert err["code"] == "NOT_FOUND"
    assert alt_soul in err["message"]


def test_soul_pin_survives_engine_restart(services, server, tmp_path, alt_soul) -> None:
    session_id = _create_chat(server, tmp_path)
    _ok(server.handle_line(_req("s", "session.soul.set", {"ref": session_id, "id": alt_soul})))
    server.close()
    fresh = EngineServer(services, user_home=tmp_path / "home")
    try:
        session = _ok(fresh.handle_line(_req("g", "session.get", {"ref": session_id})))["session"]
        assert session["soul_id"] == alt_soul
        effective = _ok(fresh.handle_line(_req("e", "soul.get_effective", {"ref": session_id})))
        assert (effective["soul_id"], effective["source"]) == (alt_soul, "session")
    finally:
        fresh.close()


def test_subagents_stay_functional() -> None:
    # Subagents run scoped policy, never the session personality —
    # the default carries no Soul and the CLI runner passes soul="".
    assert SubagentRuntimeConfig.__dataclass_fields__["soul"].default == ""
