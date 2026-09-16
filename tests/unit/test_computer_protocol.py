"""Engine protocol slice: graphic-control methods (computer.use, phase 3b)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.computer.backend import FakeBackend
from rinari.computer.service import GraphicControlService
from rinari.engine_protocol import protocol as protocol_module
from rinari.engine_protocol.server import EngineServer


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
    yield EngineServer(services, user_home=tmp_path / "home")


def _session(services, tmp_path: Path):
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    return services.sessions.start(cwd, forced_chat=True).session.id


def test_capability_flag() -> None:
    assert protocol_module.CAPABILITIES["computer_control_v1"] is True


def test_state_unknown_session(server) -> None:
    result = server._turns.computer_state({"session_id": "missing"})
    assert result["available"] is False
    assert result["session_id"] == "missing"
    assert result["error"]


def test_state_without_service(server, services, tmp_path) -> None:
    sid = _session(services, tmp_path)
    result = server._turns.computer_state({"session_id": sid})
    assert result["available"] is False
    assert "no graphic-control service" in result["error"]


def test_grant_issue_without_backend(server, services, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RINARI_COMPUTER_LAB", raising=False)
    sid = _session(services, tmp_path)
    result = server._turns.computer_grant_issue(
        {"session_id": sid, "target": "hwnd:1", "scopes": ["observe"], "ttl_s": 300}
    )
    assert result["granted"] is False
    assert result["error"]


def _attach(server, services, sid: str) -> GraphicControlService:
    service = GraphicControlService(
        session_id=sid, backend=FakeBackend(), artifact_store=services.artifacts
    )
    server._turns._desktop_computer[sid] = service
    return service


def test_grant_lifecycle(server, services, tmp_path) -> None:
    sid = _session(services, tmp_path)
    _attach(server, services, sid)
    issued = server._turns.computer_grant_issue(
        {
            "session_id": sid,
            "target": "lab-app",
            "scopes": ["observe", "input", "send"],
            "ttl_s": 300,
            "note": "panel",
        }
    )
    assert issued["granted"] is True
    assert issued["target"] == "lab-app"
    assert issued["scopes"] == ["input", "observe", "send"]
    assert 0 < issued["expires_in_s"] <= 300
    state = server._turns.computer_state({"session_id": sid})
    assert state["available"] is True
    assert state["backend"] == "fake"
    assert len(state["grants"]) == 1
    assert state["grants"][0]["grant_id"] == issued["grant_id"]
    revoked = server._turns.computer_grant_revoke(
        {"session_id": sid, "grant_id": issued["grant_id"]}
    )
    assert revoked["revoked"] is True
    assert server._turns.computer_state({"session_id": sid})["grants"] == []


def test_grant_issue_validation(server, services, tmp_path) -> None:
    sid = _session(services, tmp_path)
    _attach(server, services, sid)
    base = {"session_id": sid, "target": "lab-app", "scopes": ["observe"], "ttl_s": 300}
    for params in (
        {**base, "target": ""},
        {**base, "scopes": []},
        {**base, "scopes": ["fly"]},
        {**base, "ttl_s": 5},
        {**base, "ttl_s": 99999},
        {**base, "ttl_s": "soon"},
    ):
        result = server._turns.computer_grant_issue(params)
        assert result["granted"] is False, params
        assert result["error"], params


def test_grant_revoke_unknown(server, services, tmp_path) -> None:
    sid = _session(services, tmp_path)
    _attach(server, services, sid)
    result = server._turns.computer_grant_revoke({"session_id": sid, "grant_id": "nope"})
    assert result["revoked"] is False
    result = server._turns.computer_grant_revoke({"session_id": sid})
    assert result["revoked"] is False
    assert result["error"]


def test_wire_envelopes_end_to_end(server, services, tmp_path) -> None:
    import json

    sid = _session(services, tmp_path)
    _attach(server, services, sid)

    def call(mid: str, method: str, params: dict):
        return server.handle_line(json.dumps({"id": mid, "method": method, "params": params}))

    result = call("1", "computer.state.get", {"session_id": sid})
    assert result["id"] == "1"
    assert result["ok"] is True
    assert result["result"]["available"] is True
    assert result["result"]["backend"] == "fake"
    assert result["result"]["grants"] == []
    result = call(
        "2",
        "computer.grant.issue",
        {"session_id": sid, "target": "lab-app", "scopes": ["observe"], "ttl_s": 120},
    )
    assert result["ok"] is True
    assert result["result"]["granted"] is True
    grant_id = result["result"]["grant_id"]
    result = call("3", "computer.state.get", {"session_id": sid})
    assert [g["grant_id"] for g in result["result"]["grants"]] == [grant_id]
    result = call("4", "computer.grant.revoke", {"session_id": sid, "grant_id": grant_id})
    assert result["ok"] is True
    assert result["result"]["revoked"] is True
    result = call("5", "computer.state.get", {"session_id": sid})
    assert result["result"]["grants"] == []
    unknown = call("6", "computer.nope", {})
    assert unknown["ok"] is False
