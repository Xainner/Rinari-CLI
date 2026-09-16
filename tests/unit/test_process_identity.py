"""process_identity_v1: strong instance identity, stop preconditions,
paginated listing, ended_at/exit_reason and loopback readiness probes."""

from __future__ import annotations

import itertools
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.processes import DesktopProcesses, _exit_reason, _probe_loopback
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


def test_probe_never_resolves_dns_names():
    # Hostnames that merely start with 127. must not be dialed: resolving
    # them would turn the probe into an external scan.
    assert _probe_loopback("http://127.0.0.1.evil.com/") == "unknown"
    assert _probe_loopback("http://127.evil.com:80/") == "unknown"
    assert _probe_loopback("http://127.0.0.1.evil.com:8080/x") == "unknown"


def test_cursor_and_limit_edges(server, tmp_path):
    session_id = _create_chat(server, tmp_path, tag="edge")
    registry = ProcessRegistry()
    server.turns._desktop_processes[session_id] = registry
    try:
        assert _list(server, session_id, {"limit": 1})["result"]["total"] == 0
        for index, bad_limit in enumerate((0, 201, True, 2.0, "2")):
            response = server.handle_line(
                _req(
                    f"edge-limit-{index}",
                    "workspace.process.list",
                    {"session_id": session_id, "limit": bad_limit},
                )
            )
            assert not response["ok"] and response["error"]["code"] == "INVALID_PARAMS"
        ok_limit = server.handle_line(
            _req("edge-ok", "workspace.process.list", {"session_id": session_id, "limit": 200})
        )
        assert ok_limit["ok"]
        for index, bad_cursor in enumerate(("bogus", "o", "o-5", "o\xb2", "o" + "9" * 5000, 5, "")):
            response = server.handle_line(
                _req(
                    f"edge-cursor-{index}",
                    "workspace.process.list",
                    {"session_id": session_id, "cursor": bad_cursor},
                )
            )
            assert not response["ok"] and response["error"]["code"] == "INVALID_PARAMS"
        beyond = _list(server, session_id, {"cursor": "o999"})
        assert beyond["result"]["processes"] == []
        assert beyond["result"]["next_cursor"] is None
        assert beyond["result"]["truncated"] is False
    finally:
        for handle in registry.list():
            if handle.process.poll() is None:
                registry.kill(handle)
            registry.wait(handle, 3)


_list_counter = itertools.count()


def _list(server, session_id, params):
    merged = {"session_id": session_id, **params}
    response = server.handle_line(
        _req(f"edge-{next(_list_counter)}", "workspace.process.list", merged)
    )
    assert response["ok"], response
    return response


def test_exit_reason_branches():
    assert _exit_reason(None, False) is None
    assert _exit_reason(None, True) is None
    assert _exit_reason(0, False) == "exited"
    assert _exit_reason(3, False) == "failed"
    assert _exit_reason(-9, False) == "signaled"
    assert _exit_reason(1, True) == "stopped"
    assert _exit_reason("bogus", False) == "unknown"


def test_exited_quick_process_reports_exited(tmp_path):
    registry = ProcessRegistry()
    handle_id = registry.start([sys.executable, "-c", "pass"], cwd=str(tmp_path))
    try:
        handle = registry.get(handle_id)
        assert handle is not None
        assert registry.wait(handle, 10)
        row = _row_for(registry, handle_id)
        assert row["exit_reason"] == "exited"
        assert isinstance(row["ended_at"], float)
    finally:
        handle = registry.get(handle_id)
        if handle is not None:
            registry.wait(handle, 3)


def test_ended_at_observed_without_wait(tmp_path):
    registry = ProcessRegistry()
    handle_id = registry.start([sys.executable, "-c", "pass"], cwd=str(tmp_path))
    try:
        deadline = time.monotonic() + 10
        while registry.get(handle_id).process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        # get()/list() stamp ended_at on first observation, no wait() needed.
        assert isinstance(registry.get(handle_id).ended_at, float)
        assert isinstance(registry.list()[0].ended_at, float)
    finally:
        handle = registry.get(handle_id)
        if handle is not None:
            registry.wait(handle, 3)


def _row_for(registry, handle_id):
    from types import SimpleNamespace

    shown = SimpleNamespace(show=lambda ref: ref)
    fake_server = SimpleNamespace(
        _services=SimpleNamespace(sessions=shown),
        _turns=SimpleNamespace(_desktop_processes={"s": registry}),
        _pty=SimpleNamespace(list=lambda: []),
        _previews=SimpleNamespace(_lock=threading.RLock(), _items={}),
        _engine_instance_id="boot",
    )
    rows = DesktopProcesses(fake_server).list({"session_id": "s"})["processes"]
    return {row["id"]: row for row in rows}[f"process:{handle_id}"]


def test_bad_precondition_subtypes_rejected(server, tmp_path):
    session_id = _create_chat(server, tmp_path, tag="subtype")
    registry = ProcessRegistry()
    server.turns._desktop_processes[session_id] = registry
    handle_id = _start_sleep(registry, tmp_path)
    identity = f"process:{handle_id}"
    try:
        for index, params in enumerate(
            (
                {"generation": True},
                {"generation": 1.0},
                {"engine_instance_id": ""},
            )
        ):
            response = server.handle_line(
                _req(
                    f"sub-{index}",
                    "workspace.process.stop",
                    {"session_id": session_id, "id": identity, **params},
                )
            )
            assert not response["ok"] and response["error"]["code"] == "INVALID_PARAMS"
    finally:
        handle = registry.get(handle_id)
        if handle is not None:
            if handle.process.poll() is None:
                registry.kill(handle)
            registry.wait(handle, 3)


def test_not_found_wins_over_stale_preconditions(server, tmp_path):
    session_id = _create_chat(server, tmp_path, tag="precedence")
    response = server.handle_line(
        _req(
            "sub",
            "workspace.process.stop",
            {
                "session_id": session_id,
                "id": "process:proc_999",
                "engine_instance_id": "boot-from-another-world",
                "generation": 99,
            },
        )
    )
    assert not response["ok"] and response["error"]["code"] == "NOT_FOUND"


def test_pty_generation_and_rows():
    from rinari.engine_protocol.pty import EnginePtyService

    service = EnginePtyService(lambda event: None)
    assert service.handle_generation("pty_999") == 1
    service._handle_generation["pty_001"] = 4
    assert service.handle_generation("pty_001") == 4


needs_pty = pytest.mark.skipif(not hasattr(os, "openpty"), reason="PTY requires a POSIX platform")


@needs_pty
def test_pty_rows_carry_identity_fields(tmp_path):
    from rinari.engine_protocol.pty import EnginePtyService

    service = EnginePtyService(lambda event: None)
    first = service.start("exit 0", cwd=str(tmp_path), session_id="s")["pty_id"]
    second = service.start("exit 0", cwd=str(tmp_path), session_id="s")["pty_id"]
    try:
        assert service.handle_generation(second) > service.handle_generation(first) >= 1
        rows = {row["pty_id"]: row for row in service.list()}
        assert set((first, second)) <= set(rows)
        assert "ended_at" in rows[first] and "stop_requested" in rows[first]
    finally:
        service.terminate(first)
        service.terminate(second)
        service.shutdown()


def test_preview_rows_carry_identity_fields():
    from types import SimpleNamespace

    shown = SimpleNamespace(show=lambda ref: ref)
    item = SimpleNamespace(
        id="p1",
        session_id="s",
        process_id=None,
        server=object(),
        command=None,
        root="C:/Site",
        url="http://127.0.0.1:8123/",
    )
    fake_server = SimpleNamespace(
        _services=SimpleNamespace(sessions=shown),
        _turns=SimpleNamespace(_desktop_processes={}),
        _pty=SimpleNamespace(list=lambda: []),
        _previews=SimpleNamespace(_lock=threading.RLock(), _items={"p1": item}),
        _engine_instance_id="boot-A",
    )
    api = DesktopProcesses(fake_server)
    row = api.list({"session_id": "s"})["processes"][0]
    assert row["kind"] == "preview"
    assert row["generation"] == 1
    assert row["engine_instance_id"] == "boot-A"
    assert row["readiness"] in ("unknown", "listening", "not_listening")
    assert row["ended_at"] is None


def test_hello_without_instance_omits_field():
    from rinari.engine_protocol.messages import hello

    assert "engine_instance_id" not in hello()
    assert hello("abc123")["engine_instance_id"] == "abc123"


def test_loopback_literal_spellings():
    from rinari.engine_protocol.processes import _is_loopback_literal

    assert _is_loopback_literal("127.0.0.1")
    assert _is_loopback_literal("::1")
    # Strict literals only: exotic but valid spellings fail closed to
    # unknown rather than risking a misdial.
    assert not _is_loopback_literal("127.1")
    assert not _is_loopback_literal("localhost")
    assert not _is_loopback_literal("127.0.0.1.")
    assert not _is_loopback_literal("0x7f.0.0.1")
    assert not _is_loopback_literal("2130706433")
    assert not _is_loopback_literal("example.com")
    assert not _is_loopback_literal("")


def test_probe_port_zero_and_userinfo():
    # Port 0 is never a real service: must not fall back to :80.
    assert _probe_loopback("http://127.0.0.1:0/") == "not_listening"
    # Userinfo is stripped by hostname parsing; the literal IP is dialed.
    assert _probe_loopback("http://user:pass@127.0.0.1:9/") == "not_listening"


def test_cursor_ascii_and_boundary_edges(server, tmp_path):
    session_id = _create_chat(server, tmp_path, tag="cursor")
    registry = ProcessRegistry()
    server.turns._desktop_processes[session_id] = registry
    try:
        assert _list(server, session_id, {"cursor": "o0"})["result"]["total"] == 0
        assert _list(server, session_id, {"cursor": "o007"})["result"]["processes"] == []
        assert _list(server, session_id, {"cursor": "o123456"})["result"]["processes"] == []
        # U+0661 ARABIC-INDIC DIGIT ONE matches \d but must be rejected.
        for index, bad in enumerate(("o1234567", "o" + chr(0x661), "o007x")):
            response = server.handle_line(
                _req(
                    f"cursor-edge-{index}",
                    "workspace.process.list",
                    {"session_id": session_id, "cursor": bad},
                )
            )
            assert not response["ok"] and response["error"]["code"] == "INVALID_PARAMS"
    finally:
        for handle in registry.list():
            if handle.process.poll() is None:
                registry.kill(handle)
            registry.wait(handle, 3)


def test_pty_rows_surface_identity_and_end_state():
    from types import SimpleNamespace

    def pty_row(alive, code, ended_at, stop_requested):
        return {
            "pty_id": "pty_001",
            "command": "sh",
            "cwd": "C:/s",
            "alive": alive,
            "exit_code": code,
            "ended_at": ended_at,
            "stop_requested": stop_requested,
            "session_id": "s",
        }

    def fake_server(rows):
        shown = SimpleNamespace(show=lambda ref: ref)
        owner = SimpleNamespace(list=lambda: rows, handle_generation=lambda pid: 9)
        return SimpleNamespace(
            _services=SimpleNamespace(sessions=shown),
            _turns=SimpleNamespace(_desktop_processes={}),
            _pty=owner,
            _previews=SimpleNamespace(_lock=threading.RLock(), _items={}),
            _engine_instance_id="boot",
        )

    running = DesktopProcesses(fake_server([pty_row(True, None, None, False)])).list(
        {"session_id": "s"}
    )["processes"][0]
    assert running["generation"] == 9
    assert running["engine_instance_id"] == "boot"
    assert running["exit_reason"] is None
    assert running["ended_at"] is None
    assert running["readiness"] == "unknown"
    stopped = DesktopProcesses(fake_server([pty_row(False, 0, 1700000060.0, True)])).list(
        {"session_id": "s"}
    )["processes"][0]
    assert stopped["exit_reason"] == "stopped"
    assert stopped["ended_at"] == 1700000060.0


def test_preview_exited_row_reports_ended_unknown():
    from types import SimpleNamespace

    handle = SimpleNamespace(process=SimpleNamespace(poll=lambda: 3))
    item = SimpleNamespace(
        id="p1",
        session_id="s",
        process_id="h1",
        server=None,
        command="npm run dev",
        root="C:/Site",
        url=None,
    )
    shown = SimpleNamespace(show=lambda ref: ref)
    fake_server = SimpleNamespace(
        _services=SimpleNamespace(sessions=shown),
        _turns=SimpleNamespace(_desktop_processes={}),
        _pty=SimpleNamespace(list=lambda: []),
        _previews=SimpleNamespace(
            _lock=threading.RLock(),
            _items={"p1": item},
            processes=SimpleNamespace(get=lambda handle_id: handle),
        ),
        _engine_instance_id="boot",
    )
    row = DesktopProcesses(fake_server).list({"session_id": "s"})["processes"][0]
    assert row["exit_reason"] == "failed"
    assert row["ended_at"] is None
    assert row["generation"] == 1


def test_read_caches_loopback_readiness_number(tmp_path):
    from types import SimpleNamespace

    httpd = HTTPServer(("127.0.0.1", 0), _Quiet)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_port
        item = SimpleNamespace(
            id="p1",
            session_id="s",
            process_id=None,
            server=object(),
            command=None,
            root="C:/Site",
            url=f"http://127.0.0.1:{port}/",
        )
        shown = SimpleNamespace(show=lambda ref: ref)
        fake_server = SimpleNamespace(
            _services=SimpleNamespace(sessions=shown),
            _turns=SimpleNamespace(_desktop_processes={}),
            _pty=SimpleNamespace(list=lambda: []),
            _previews=SimpleNamespace(_lock=threading.RLock(), _items={"p1": item}),
            _engine_instance_id="boot",
        )
        api = DesktopProcesses(fake_server)
        # The probe runs off the dispatch thread, so the first read returns
        # the previous (absent) observation and schedules one. Reading again
        # picks up the result; nothing ever blocks handle_line on a dial.
        first = api.read({"session_id": "s", "id": "preview:p1"})["process"]
        assert first["readiness"] == "unknown"
        assert first["readiness_checked_at"] is None
        deadline = time.monotonic() + 5
        row = first
        while row["readiness"] != "listening" and time.monotonic() < deadline:
            time.sleep(0.02)
            row = api.read({"session_id": "s", "id": "preview:p1"})["process"]
        assert row["readiness"] == "listening"
        assert isinstance(row["readiness_checked_at"], float)
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


# -- exact duration: only where the engine owns real timestamps ----------


def _preview_server(items, pty_rows=None, processes=None):
    from types import SimpleNamespace

    previews = SimpleNamespace(_lock=threading.RLock(), _items=items)
    if processes is not None:
        previews.processes = processes
    return SimpleNamespace(
        _services=SimpleNamespace(sessions=SimpleNamespace(show=lambda ref: ref)),
        _turns=SimpleNamespace(_desktop_processes={}),
        _pty=SimpleNamespace(list=lambda: list(pty_rows or [])),
        _previews=previews,
        _engine_instance_id="boot",
    )


@needs_pty
def test_pty_row_exposes_real_started_at(tmp_path):
    """A live pty reports its own start time, never the caller clock."""
    from rinari.engine_protocol.pty import EnginePtyService

    before = time.time()
    service = EnginePtyService(lambda event: None)
    pty_id = service.start("sleep 30", cwd=str(tmp_path), session_id="s")["pty_id"]
    try:
        row = {item["pty_id"]: item for item in service.list()}[pty_id]
        assert isinstance(row["started_at"], float)
        assert before <= row["started_at"] <= time.time()
        assert row["ended_at"] is None
    finally:
        service.terminate(pty_id)
        service.shutdown()


@needs_pty
def test_finished_pty_reports_ended_at_after_started_at(tmp_path):
    """Both ends are real observations, so the duration is never negative."""
    from rinari.engine_protocol.pty import EnginePtyService

    service = EnginePtyService(lambda event: None)
    pty_id = service.start("exit 0", cwd=str(tmp_path), session_id="s")["pty_id"]
    try:
        deadline = time.monotonic() + 10
        row = {item["pty_id"]: item for item in service.list()}[pty_id]
        while row["ended_at"] is None and time.monotonic() < deadline:
            time.sleep(0.02)
            row = {item["pty_id"]: item for item in service.list()}[pty_id]
        assert isinstance(row["started_at"], float)
        assert isinstance(row["ended_at"], float)
        assert row["ended_at"] >= row["started_at"]
    finally:
        service.shutdown()


def test_preview_with_process_handle_propagates_timestamps():
    """A process-backed preview carries the registry handle real times."""
    from types import SimpleNamespace

    handle = SimpleNamespace(
        process=SimpleNamespace(poll=lambda: 0),
        started_at=1_700_000_000.0,
        ended_at=1_700_000_042.0,
        stop_requested=True,
    )
    item = SimpleNamespace(
        id="p1",
        session_id="s",
        process_id="h1",
        server=None,
        command="npm run dev",
        root="C:/Site",
        url=None,
    )
    fake_server = _preview_server(
        {"p1": item},
        processes=SimpleNamespace(get=lambda handle_id: handle),
    )
    row = DesktopProcesses(fake_server).list({"session_id": "s"})["processes"][0]
    assert row["started_at"] == 1_700_000_000.0
    assert row["ended_at"] == 1_700_000_042.0
    # stop_requested wins over the exit code: this end was asked for.
    assert row["exit_reason"] == "stopped"


def test_external_preview_invents_no_duration():
    """No handle means no timestamps: absent, never the current clock."""
    from types import SimpleNamespace

    item = SimpleNamespace(
        id="p1",
        session_id="s",
        process_id=None,
        server=object(),
        command=None,
        root="C:/Site",
        url=None,
    )
    row = DesktopProcesses(_preview_server({"p1": item})).list({"session_id": "s"})["processes"][0]
    assert row["started_at"] is None
    assert row["ended_at"] is None


def test_listing_sorts_rows_without_a_start_time():
    """A null started_at must not reach the unary minus in the sort key."""
    from types import SimpleNamespace

    external = SimpleNamespace(
        id="p1",
        session_id="s",
        process_id=None,
        server=object(),
        command=None,
        root="C:/Site",
        url=None,
    )
    pty_row = {
        "pty_id": "t1",
        "command": "bash",
        "cwd": "C:/Site",
        "alive": True,
        "exit_code": None,
        "started_at": 1_700_000_000.0,
        "ended_at": None,
        "stop_requested": False,
        "session_id": "s",
    }
    result = DesktopProcesses(_preview_server({"p1": external}, pty_rows=[pty_row])).list(
        {"session_id": "s"}
    )
    assert result["total"] == 2
    # Running first; the timeless external preview sorts after it.
    assert [row["id"] for row in result["processes"]] == ["pty:t1", "preview:p1"]


def test_readiness_probe_never_runs_on_the_calling_thread():
    """A row must not block on a dial: handle_line owns the stdin loop."""
    from types import SimpleNamespace

    from rinari.engine_protocol import processes as processes_module

    item = SimpleNamespace(
        id="p1",
        session_id="s",
        process_id=None,
        server=object(),
        command=None,
        root="C:/Site",
        url="http://127.0.0.1:9/",
    )
    caller = threading.get_ident()
    seen: list[int] = []
    original = processes_module._probe_loopback

    def recording(url):
        seen.append(threading.get_ident())
        return original(url)

    processes_module._probe_loopback = recording
    try:
        api = DesktopProcesses(_preview_server({"p1": item}))
        started = time.monotonic()
        row = api.list({"session_id": "s"})["processes"][0]
        # The dial is not awaited, so listing stays under the probe timeout.
        assert time.monotonic() - started < processes_module.READINESS_TIMEOUT_S
        assert row["readiness"] == "unknown"
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen and caller not in seen
    finally:
        processes_module._probe_loopback = original


def test_readiness_cache_stays_bounded():
    """Preview ids are random per start: the cache must not grow forever."""
    from rinari.engine_protocol import processes as processes_module

    api = DesktopProcesses(_preview_server({}))
    with api._readiness_lock:
        for index in range(processes_module.READINESS_CACHE_MAX + 20):
            api._readiness[f"preview:{index}"] = ("listening", float(index))
        api._readiness_inflight.add("preview:new")
    original = processes_module._probe_loopback
    processes_module._probe_loopback = lambda url: "listening"
    try:
        api._probe_into_cache("preview:new", "http://127.0.0.1:1/")
    finally:
        processes_module._probe_loopback = original
    assert len(api._readiness) == processes_module.READINESS_CACHE_MAX
    assert "preview:new" in api._readiness
    # Oldest checked_at evicted first.
    assert "preview:0" not in api._readiness
