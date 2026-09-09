"""Engine PTY sessions: start/output/exit events, lifecycle (docs/desktop 05-A).

Same PtyRegistry backend as the tool layer, promoted to engine-owned
sessions. POSIX-only flows skip on Windows; the unsupported-code path is
pinned on every platform.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.pty import EnginePtyService
from rinari.engine_protocol.server import EngineServer
from rinari.tools.native.ptytools import PtyRegistry

needs_pty = pytest.mark.skipif(not hasattr(os, "openpty"), reason="PTY requires a POSIX platform")


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


def _wait_for(server, event_type, pty_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for evt in server.drain_events():
            payload = evt.get("payload", {})
            if evt.get("event") == event_type and payload.get("pty_id") == pty_id:
                return payload
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {event_type} on {pty_id}")


def test_unsupported_platform_reports_machine_code(services, monkeypatch) -> None:
    monkeypatch.setattr(PtyRegistry, "supported", False)
    service = EnginePtyService(lambda payload: None, home=None)
    err = None
    try:
        service.start("echo hi", cwd="/tmp")
    except Exception as exc:
        err = exc
    assert err is not None
    assert getattr(err, "code", None) == "PTY_UNSUPPORTED"


@needs_pty
def test_start_runs_and_emits_output_then_exit(server, tmp_path) -> None:
    started = _ok(
        server.handle_line(
            _req("p1", "pty.start", {"command": "printf 'hello-pty\\n'", "cwd": str(tmp_path)})
        )
    )
    pty_id = started["pty_id"]
    assert pty_id.startswith("pty_")
    assert started["session_id"] is None

    outputs = []
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        for evt in server.drain_events():
            payload = evt.get("payload", {})
            if payload.get("pty_id") != pty_id:
                continue
            if evt.get("event") == "pty.output":
                outputs.append(payload["data"])
            if evt.get("event") == "pty.exit":
                assert payload["exit_code"] == 0
                assert "hello-pty" in "".join(outputs)
                # Exactly one exit per pty: nothing else arrives afterwards.
                time.sleep(0.5)
                leftovers = [
                    e for e in server.drain_events() if e.get("payload", {}).get("pty_id") == pty_id
                ]
                assert leftovers == []
                return
        time.sleep(0.2)
    raise AssertionError("timed out waiting for pty.exit")


@needs_pty
def test_write_resize_read_terminate_flow(server, tmp_path) -> None:
    pty_id = _ok(
        server.handle_line(_req("p1", "pty.start", {"command": "cat", "cwd": str(tmp_path)}))
    )["pty_id"]

    written = _ok(server.handle_line(_req("p2", "pty.write", {"pty_id": pty_id, "data": "hi\n"})))
    assert written["written"] > 0
    resized = _ok(
        server.handle_line(_req("p3", "pty.resize", {"pty_id": pty_id, "columns": 100, "rows": 30}))
    )
    assert (resized["columns"], resized["rows"]) == (100, 30)

    payload = _wait_for(server, "pty.output", pty_id)
    assert "hi" in payload["data"]

    snapshot = _ok(server.handle_line(_req("p4", "pty.read", {"pty_id": pty_id})))
    assert snapshot["alive"] is True
    assert "hi" in snapshot["data"]

    done = _ok(server.handle_line(_req("p5", "pty.terminate", {"pty_id": pty_id})))
    assert done["alive"] is False
    assert done["exit_code"] is not None
    # Terminating a dead handle is a no-op success, never a tombstone error.
    again = _ok(server.handle_line(_req("p6", "pty.terminate", {"pty_id": pty_id})))
    assert again["alive"] is False

    listed = _ok(server.handle_line(_req("p7", "pty.list", {})))["ptys"]
    assert any(p["pty_id"] == pty_id and p["alive"] is False for p in listed)


def test_unknown_handle_is_not_found(server) -> None:
    for method, params in (
        ("pty.write", {"pty_id": "pty_999", "data": "x"}),
        ("pty.resize", {"pty_id": "pty_999", "columns": 80, "rows": 24}),
        ("pty.read", {"pty_id": "pty_999"}),
        ("pty.terminate", {"pty_id": "pty_999"}),
    ):
        err = _err(server.handle_line(_req(f"q-{method}", method, params)))
        assert err["code"] == "NOT_FOUND", (method, err)


def test_start_validation(server, tmp_path) -> None:
    err = _err(server.handle_line(_req("v1", "pty.start", {"command": "  ", "cwd": str(tmp_path)})))
    assert err["code"] == "INVALID_PARAMS"
    err = _err(server.handle_line(_req("v2", "pty.start", {"command": "echo hi"})))
    assert err["code"] == "INVALID_PARAMS"
    err = _err(
        server.handle_line(
            _req("v3", "pty.start", {"command": "echo hi", "cwd": str(tmp_path / "nope")})
        )
    )
    assert err["code"] == "INVALID_PARAMS"
    err = _err(
        server.handle_line(
            _req("v4", "pty.start", {"command": "echo hi", "cwd": str(tmp_path), "env": ["x"]})
        )
    )
    assert err["code"] == "INVALID_PARAMS"
    # The home root is never an implicit PTY workspace (same rule as projects).
    err = _err(
        server.handle_line(
            _req("v5", "pty.start", {"command": "echo hi", "cwd": str(tmp_path / "home")})
        )
    )
    assert err["code"] == "PERMISSION_DENIED"


@needs_pty
def test_session_binding_defaults_cwd(services, server, tmp_path) -> None:
    session_id = _ok(
        server.handle_line(_req("c", "session.create", {"cwd": str(tmp_path), "chat": True}))
    )["session"]["id"]
    started = _ok(
        server.handle_line(_req("p1", "pty.start", {"command": "pwd", "session_id": session_id}))
    )
    assert started["session_id"] == session_id
    payload = _wait_for(server, "pty.output", started["pty_id"])
    assert payload["session_id"] == session_id
    assert str(tmp_path) in payload["data"]
    listed = _ok(server.handle_line(_req("p2", "pty.list", {})))["ptys"]
    assert any(p["session_id"] == session_id for p in listed)


@needs_pty
def test_restart_reports_no_ghost_handles(services, server, tmp_path) -> None:
    pty_id = _ok(
        server.handle_line(_req("p1", "pty.start", {"command": "sleep 30", "cwd": str(tmp_path)}))
    )["pty_id"]
    assert any(
        p["pty_id"] == pty_id for p in _ok(server.handle_line(_req("p2", "pty.list", {})))["ptys"]
    )
    server.close()
    fresh = EngineServer(services, user_home=tmp_path / "home")
    try:
        assert _ok(fresh.handle_line(_req("p3", "pty.list", {})))["ptys"] == []
    finally:
        fresh.close()
