"""process_identity_v1: strong instance identity, stop preconditions,
paginated listing, ended_at/exit_reason and loopback readiness probes."""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.processes import DesktopProcesses, _probe_loopback
from rinari.engine_protocol.protocol import CAPABILITIES
from rinari.engine_protocol.server import EngineServer
from rinari.tools.native.process import ProcessRegistry


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


def _create_chat(server, tmp_path, tag="t"):
    response = server.handle_line(
        _req(f"{tag}-c", "session.create", {"cwd": str(tmp_path), "chat": True})
    )
    assert response is not None and response["ok"] is True
    return response["result"]["session"]["id"]


def _start_sleep(registry, tmp_path, seconds=30):
    return registry.start(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        cwd=str(tmp_path),
    )


def test_capability_advertised():
    assert CAPABILITIES.get("process_identity_v1") is True


def test_instance_id_stable_per_boot_and_visible(server, tmp_path):
    first = server.hello()["engine_instance_id"]
    assert isinstance(first, str) and len(first) >= 16
    assert server.hello()["engine_instance_id"] == first
    info = server.handle_line(_req("i", "engine.info"))
    assert info["ok"] and info["result"]["engine_instance_id"] == first
    session_id = _create_chat(server, tmp_path, tag="inst")
    listed = server.handle_line(_req("l", "workspace.process.list", {"session_id": session_id}))
    assert listed["ok"] and listed["result"]["engine_instance_id"] == first


def test_restart_between_confirm_and_execute_is_rejected(server, services, tmp_path):
    """Observe on boot A, restart, then act on boot B with A's identity."""
    session_id = _create_chat(server, tmp_path, tag="old")
    registry = ProcessRegistry()
    server.turns._desktop_processes[session_id] = registry
    handle_id = _start_sleep(registry, tmp_path)
    identity = f"process:{handle_id}"
    observed = server.handle_line(_req("l", "workspace.process.list", {"session_id": session_id}))[
        "result"
    ]["processes"][0]
    assert observed["id"] == identity
    old_instance = observed["engine_instance_id"]
    old_generation = observed["generation"]

    restarted = EngineServer(services, user_home=tmp_path / "home")
    try:
        assert restarted.hello()["engine_instance_id"] != old_instance
        # Same sequential id in the new boot, different resource.
        registry2 = ProcessRegistry()
        handle_id2 = _start_sleep(registry2, tmp_path)
        assert handle_id2 == handle_id
        restarted.turns._desktop_processes[session_id] = registry2
        stale = restarted.handle_line(
            _req(
                "s",
                "workspace.process.stop",
                {
                    "session_id": session_id,
                    "id": identity,
                    "engine_instance_id": old_instance,
                    "generation": old_generation,
                },
            )
        )
        assert not stale["ok"] and stale["error"]["code"] == "STALE_RESOURCE"
        # The new resource is still running: nothing was killed blindly.
        handle2 = registry2.get(handle_id2)
        assert handle2 is not None and handle2.process.poll() is None
    finally:
        for reg in (registry, registry2):
            for handle in reg.list():
                if handle.process.poll() is None:
                    reg.kill(handle)
                reg.wait(handle, 3)
        restarted.close()


def test_generation_distinguishes_registry_incarnations():
    first, second = ProcessRegistry(), ProcessRegistry()
    assert first.generation != second.generation


def test_stop_with_fresh_preconditions_succeeds(server, tmp_path):
    session_id = _create_chat(server, tmp_path, tag="fresh")
    registry = ProcessRegistry()
    server.turns._desktop_processes[session_id] = registry
    handle_id = _start_sleep(registry, tmp_path)
    identity = f"process:{handle_id}"
    try:
        observed = server.handle_line(
            _req("l", "workspace.process.list", {"session_id": session_id})
        )["result"]["processes"][0]
        stopped = server.handle_line(
            _req(
                "s",
                "workspace.process.stop",
                {
                    "session_id": session_id,
                    "id": identity,
                    "engine_instance_id": observed["engine_instance_id"],
                    "generation": observed["generation"],
                },
            )
        )
        assert stopped["ok"] and not stopped["result"]["running"]
    finally:
        handle = registry.get(handle_id)
        if handle is not None:
            if handle.process.poll() is None:
                registry.kill(handle)
            registry.wait(handle, 3)


def test_stop_precondition_types_are_validated(server, tmp_path):
    session_id = _create_chat(server, tmp_path, tag="types")
    registry = ProcessRegistry()
    server.turns._desktop_processes[session_id] = registry
    handle_id = _start_sleep(registry, tmp_path)
    identity = f"process:{handle_id}"
    try:
        bad_generation = server.handle_line(
            _req(
                "s1",
                "workspace.process.stop",
                {"session_id": session_id, "id": identity, "generation": "1"},
            )
        )
        assert not bad_generation["ok"] and bad_generation["error"]["code"] == "INVALID_PARAMS"
        bad_instance = server.handle_line(
            _req(
                "s2",
                "workspace.process.stop",
                {"session_id": session_id, "id": identity, "engine_instance_id": 123},
            )
        )
        assert not bad_instance["ok"] and bad_instance["error"]["code"] == "INVALID_PARAMS"
    finally:
        handle = registry.get(handle_id)
        if handle is not None:
            if handle.process.poll() is None:
                registry.kill(handle)
            registry.wait(handle, 3)


def test_stale_generation_is_rejected_without_precondition_allows_legacy(server, tmp_path):
    from types import SimpleNamespace

    shown = SimpleNamespace(show=lambda ref: ref)
    turns = SimpleNamespace(_desktop_processes={})
    pty = SimpleNamespace(list=lambda: [])
    import threading as threading_module

    previews = SimpleNamespace(_lock=threading_module.RLock(), _items={}, _instance="boot-A")
    fake_server = SimpleNamespace(
        _services=SimpleNamespace(sessions=shown),
        _turns=turns,
        _pty=pty,
        _previews=previews,
        _engine_instance_id="boot-A",
    )
    api = DesktopProcesses(fake_server)
    registry = SimpleNamespace(
        generation=7,
        list=lambda: [
            SimpleNamespace(
                id="proc_001",
                command="worker",
                cwd="C:/s",
                started_at=1.0,
                process=SimpleNamespace(pid=1, poll=lambda: None),
            )
        ],
    )
    turns._desktop_processes["s"] = registry
    row = api.list({"session_id": "s"})["processes"][0]
    assert row["generation"] == 7
    assert row["engine_instance_id"] == "boot-A"
    with pytest.raises(EngineProtocolError) as exc:
        api.stop({"session_id": "s", "id": row["id"], "generation": 8})
    assert exc.value.code == "STALE_RESOURCE"


def test_paginated_listing(server, tmp_path):
    session_id = _create_chat(server, tmp_path, tag="page")
    registry = ProcessRegistry()
    server.turns._desktop_processes[session_id] = registry
    ids = [_start_sleep(registry, tmp_path) for _ in range(3)]
    try:
        first = server.handle_line(
            _req("p1", "workspace.process.list", {"session_id": session_id, "limit": 2})
        )
        assert first["ok"]
        result = first["result"]
        assert result["total"] == 3
        assert len(result["processes"]) == 2
        assert result["truncated"] is True
        assert result["next_cursor"] is not None
        second = server.handle_line(
            _req(
                "p2",
                "workspace.process.list",
                {"session_id": session_id, "limit": 2, "cursor": result["next_cursor"]},
            )
        )
        assert second["ok"]
        assert len(second["result"]["processes"]) == 1
        assert second["result"]["next_cursor"] is None
        assert second["result"]["truncated"] is False
        seen = {row["id"] for row in result["processes"]} | {
            row["id"] for row in second["result"]["processes"]
        }
        assert seen == {f"process:{handle}" for handle in ids}
        bad_cursor = server.handle_line(
            _req("p3", "workspace.process.list", {"session_id": session_id, "cursor": "bogus"})
        )
        assert not bad_cursor["ok"] and bad_cursor["error"]["code"] == "INVALID_PARAMS"
        bad_limit = server.handle_line(
            _req("p4", "workspace.process.list", {"session_id": session_id, "limit": 0})
        )
        assert not bad_limit["ok"] and bad_limit["error"]["code"] == "INVALID_PARAMS"
    finally:
        for handle in registry.list():
            if handle.process.poll() is None:
                registry.kill(handle)
            registry.wait(handle, 3)


def test_ended_at_and_exit_reason_lifecycle(tmp_path):
    registry = ProcessRegistry()
    quick = registry.start([sys.executable, "-c", "import sys; sys.exit(3)"], cwd=str(tmp_path))
    handle = registry.get(quick)
    assert handle is not None and handle.ended_at is None
    assert registry.wait(handle, 10)
    assert handle.exit_code == 3
    assert isinstance(handle.ended_at, float) and handle.ended_at >= handle.started_at

    from types import SimpleNamespace

    shown = SimpleNamespace(show=lambda ref: ref)
    fake_server = SimpleNamespace(
        _services=SimpleNamespace(sessions=shown),
        _turns=SimpleNamespace(_desktop_processes={"s": registry}),
        _pty=SimpleNamespace(list=lambda: []),
        _previews=SimpleNamespace(_lock=__import__("threading").RLock(), _items={}),
        _engine_instance_id="boot",
    )
    api = DesktopProcesses(fake_server)
    rows = {row["id"]: row for row in api.list({"session_id": "s"})["processes"]}
    assert rows[f"process:{quick}"]["exit_reason"] == "failed"

    sleeper = _start_sleep(registry, tmp_path)
    try:
        before = {row["id"]: row for row in api.list({"session_id": "s"})["processes"]}[
            f"process:{sleeper}"
        ]
        assert before["exit_reason"] is None and before["ended_at"] is None
        assert api.stop({"session_id": "s", "id": f"process:{sleeper}"}) == {
            "id": f"process:{sleeper}",
            "running": False,
        }
        after = {row["id"]: row for row in api.list({"session_id": "s"})["processes"]}[
            f"process:{sleeper}"
        ]
        assert after["exit_reason"] == "stopped"
        assert isinstance(after["ended_at"], float)
    finally:
        target = registry.get(sleeper)
        if target is not None:
            if target.process.poll() is None:
                registry.kill(target)
            registry.wait(target, 3)


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()


def test_readiness_probe_reports_loopback_truthfully(tmp_path):
    httpd = HTTPServer(("127.0.0.1", 0), _Quiet)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_port
        assert _probe_loopback(f"http://127.0.0.1:{port}/") == "listening"
        assert _probe_loopback("https://example.com/") == "unknown"
        assert _probe_loopback("not-a-url") == "unknown"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
    closed = HTTPServer(("127.0.0.1", 0), _Quiet)
    dead_port = closed.server_port
    closed.server_close()
    deadline = time.monotonic() + 5
    state = "listening"
    while state == "listening" and time.monotonic() < deadline:
        state = _probe_loopback(f"http://127.0.0.1:{dead_port}/")
        time.sleep(0.05)
    assert state == "not_listening"


def test_rows_carry_unknown_defaults_without_urls(tmp_path):
    registry = ProcessRegistry()
    handle_id = _start_sleep(registry, tmp_path)
    try:
        from types import SimpleNamespace

        shown = SimpleNamespace(show=lambda ref: ref)
        fake_server = SimpleNamespace(
            _services=SimpleNamespace(sessions=shown),
            _turns=SimpleNamespace(_desktop_processes={"s": registry}),
            _pty=SimpleNamespace(list=lambda: []),
            _previews=SimpleNamespace(_lock=__import__("threading").RLock(), _items={}),
            _engine_instance_id="boot",
        )
        api = DesktopProcesses(fake_server)
        row = api.list({"session_id": "s"})["processes"][0]
        assert row["id"] == f"process:{handle_id}"
        assert row["readiness"] == "unknown"
        assert row["readiness_checked_at"] is None
    finally:
        handle = registry.get(handle_id)
        if handle is not None:
            if handle.process.poll() is None:
                registry.kill(handle)
            registry.wait(handle, 3)
