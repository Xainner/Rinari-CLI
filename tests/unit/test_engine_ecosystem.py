"""Engine Protocol slice 9: MCP / plugins / tools / policy reads."""

from __future__ import annotations

import json

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
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


def test_mcp_crud_and_test(server) -> None:
    assert _ok(server.handle_line(_req("m1", "mcp.list", {})))["servers"] == []
    created = _ok(
        server.handle_line(
            _req(
                "m2",
                "mcp.create",
                {"name": "demo", "command": ["not-a-real-mcp-binary-xyz"]},
            )
        )
    )["server"]
    assert created["connected"] is False
    assert created["enabled"] is True
    # Plain secret values are rejected at the service boundary.
    assert (
        _err(
            server.handle_line(
                _req(
                    "m3",
                    "mcp.create",
                    {
                        "name": "bad",
                        "command": ["x"],
                        "env_refs": {"TOKEN": "plain-secret"},
                    },
                )
            )
        )["code"]
        == "INVALID_PARAMS"
    )
    listed = _ok(server.handle_line(_req("m4", "mcp.list", {})))["servers"]
    assert [s["name"] for s in listed] == ["demo"]
    disabled = _ok(server.handle_line(_req("m5", "mcp.disable", {"name": "demo"})))["server"]
    assert disabled["enabled"] is False
    enabled = _ok(server.handle_line(_req("m6", "mcp.enable", {"name": "demo"})))["server"]
    assert enabled["enabled"] is True
    # Unlaunchable binary: hermetic failure, reported — never raised.
    test = _ok(server.handle_line(_req("m7", "mcp.test", {"name": "demo"})))["test"]
    assert test["ok"] is False
    removed = _ok(server.handle_line(_req("m8", "mcp.remove", {"name": "demo"})))
    assert removed["removed"] == {"name": "demo"}
    assert _err(server.handle_line(_req("m9", "mcp.get", {"name": "demo"})))["code"] == "NOT_FOUND"
    # Unknown server test reports the MCP code inside a successful envelope.
    unknown = _ok(server.handle_line(_req("m10", "mcp.test", {"name": "nope"})))["test"]
    assert unknown["ok"] is False
    assert unknown["error"] == "MCP_NOT_FOUND"


def test_plugins_tools_policy(server) -> None:
    assert _ok(server.handle_line(_req("p1", "plugin.list", {})))["plugins"] == []
    assert _ok(server.handle_line(_req("p2", "plugin.diagnostics", {})))["reports"] == []
    assert (
        _err(server.handle_line(_req("p3", "plugin.enable", {"name": "nope", "source": "user"})))[
            "code"
        ]
        == "NOT_FOUND"
    )
    tools = _ok(server.handle_line(_req("t1", "tool.list", {})))["tools"]
    assert len(tools) > 0
    names = {t["name"] for t in tools}
    assert "artifact.read" in names and "browser.click" in names
    policy = _ok(server.handle_line(_req("g1", "policy.get", {})))
    assert policy["mode_profile"]["plan"] != policy["mode_profile"]["build"]
    assert policy["mode_profile"]["plan"] == policy["mode_profile"]["review"]
