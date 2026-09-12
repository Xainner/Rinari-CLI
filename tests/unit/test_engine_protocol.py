"""Engine Protocol slice 1: framing, dispatcher, session methods, snapshot (TDD)."""

from __future__ import annotations

import io
import json

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol import protocol
from rinari.engine_protocol.dispatcher import EngineDispatcher
from rinari.engine_protocol.messages import event, failure, hello, success
from rinari.engine_protocol.server import EngineServer
from rinari.engine_protocol.transports.stdio import run_stdio


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
def server(services):
    return EngineServer(services)


def _req(request_id, method, params=None):
    line: dict = {"id": request_id, "method": method}
    if params is not None:
        line["params"] = params
    return json.dumps(line)


# -- envelopes --------------------------------------------------------------


def test_hello_shape() -> None:
    payload = hello()
    assert payload["type"] == "hello"
    assert payload["protocol"] == protocol.PROTOCOL_NAME
    assert payload["protocol_version"] == protocol.PROTOCOL_VERSION
    assert payload["engine_version"]
    assert payload["capabilities"]["chat"] is True


def test_success_and_failure_envelopes() -> None:
    ok = success("req_1", {"a": 1})
    assert ok == {"id": "req_1", "ok": True, "result": {"a": 1}}
    bad = failure("req_2", "NOPE", "no", retryable=True, details={"k": "v"})
    assert bad["id"] == "req_2"
    assert bad["ok"] is False
    assert bad["error"]["code"] == "NOPE"
    assert bad["error"]["retryable"] is True
    assert bad["error"]["details"] == {"k": "v"}


def test_event_envelope() -> None:
    assert event("turn.started", {"session_id": "s"}) == {
        "type": "event",
        "event": "turn.started",
        "payload": {"session_id": "s"},
    }


# -- dispatcher -------------------------------------------------------------


def test_unknown_method() -> None:
    dispatcher = EngineDispatcher()
    response = dispatcher.dispatch(_req("r1", "nope.unknown"))
    assert response is not None
    assert response["id"] == "r1"
    assert response["ok"] is False
    assert response["error"]["code"] == "UNKNOWN_METHOD"


def test_duplicate_request_id_rejected() -> None:
    calls: list = []
    dispatcher = EngineDispatcher()
    dispatcher.register("echo", lambda params: calls.append(params) or {})
    first = dispatcher.dispatch(_req("dup", "echo", {"n": 1}))
    second = dispatcher.dispatch(_req("dup", "echo", {"n": 2}))
    assert first is not None and first["ok"] is True
    assert second is not None and second["ok"] is False
    assert second["error"]["code"] == "DUPLICATE_REQUEST_ID"
    assert calls == [{"n": 1}]


def test_malformed_and_broken_frames() -> None:
    dispatcher = EngineDispatcher()
    assert dispatcher.dispatch("") is None
    assert dispatcher.dispatch("   \n") is None
    broken = dispatcher.dispatch("{not json")
    assert broken is not None and broken["ok"] is False
    assert broken["error"]["code"] == "BROKEN_FRAME"
    missing_id = dispatcher.dispatch(json.dumps({"method": "engine.info"}))
    assert missing_id is not None and missing_id["ok"] is False
    assert missing_id["error"]["code"] == "MALFORMED_REQUEST"
    bad_params = dispatcher.dispatch(json.dumps({"id": "x", "method": "engine.info", "params": []}))
    assert bad_params is not None and bad_params["ok"] is False
    assert bad_params["error"]["code"] == "INVALID_PARAMS"


# -- server: engine + sessions ----------------------------------------------


def test_engine_info(server) -> None:
    response = server.handle_line(_req("r1", "engine.info"))
    assert response is not None and response["ok"] is True
    info = response["result"]
    assert info["protocol_version"] == 1
    assert info["capabilities"]["projects"] is True


def test_personal_memory_protocol_is_engine_authority(server) -> None:
    info = server.handle_line(_req("mem-info", "engine.info"))
    assert info["result"]["capabilities"]["personal_memory_v1"] is True

    created = server.handle_line(
        _req(
            "mem-create",
            "memory.remember",
            {"topic": "language", "text": "Prefers Spanish", "kind": "preference"},
        )
    )
    assert created["ok"] is True
    record = created["result"]["record"]
    assert record["revision"] == 1

    listed = server.handle_line(_req("mem-list", "memory.list", {"limit": 10}))
    assert listed["result"]["count"] == 1
    found = server.handle_line(_req("mem-search", "memory.search", {"query": "Spanish"}))
    assert found["result"]["records"][0]["id"] == record["id"]

    updated = server.handle_line(
        _req(
            "mem-update",
            "memory.update",
            {"id": record["id"], "expected_revision": 1, "text": "Prefers Spanish responses"},
        )
    )
    assert updated["ok"] is True
    assert updated["result"]["record"]["revision"] == 2
    stale = server.handle_line(
        _req(
            "mem-stale",
            "memory.update",
            {"id": record["id"], "expected_revision": 1, "text": "stale"},
        )
    )
    assert stale["error"]["code"] == "CONFLICT"

    forgotten = server.handle_line(
        _req(
            "mem-forget",
            "memory.forget",
            {"id": record["id"], "expected_revision": 2},
        )
    )
    assert forgotten["result"]["forgotten"] is True
    missing = server.handle_line(_req("mem-missing", "memory.get", {"id": record["id"]}))
    assert missing["error"]["code"] == "NOT_FOUND"
    recreated = server.handle_line(
        _req(
            "mem-recreate",
            "memory.remember",
            {"topic": "language", "text": "Prefers Spanish responses", "kind": "preference"},
        )
    )
    assert recreated["error"]["code"] == "INVALID_USAGE"


def test_personal_memory_protocol_rejects_unsafe_fields(server) -> None:
    unknown = server.handle_line(
        _req("mem-unknown", "memory.remember", {"topic": "x", "text": "y", "scope": "project"})
    )
    assert unknown["error"]["code"] == "INVALID_PARAMS"
    secret_topic = server.handle_line(
        _req("mem-secret", "memory.remember", {"topic": "password=longsecret123", "text": "x"})
    )
    assert secret_topic["ok"] is False


def test_session_create_get_list_roundtrip(server, tmp_path) -> None:
    created = server.handle_line(
        _req("c1", "session.create", {"cwd": str(tmp_path), "chat": True, "title": "proto"})
    )
    assert created is not None and created["ok"] is True
    session = created["result"]["session"]
    assert session["kind"] == "CHAT"
    assert session["title"] == "proto"
    assert created["result"]["created"] is True

    fetched = server.handle_line(_req("c2", "session.get", {"ref": session["id"]}))
    assert fetched is not None and fetched["ok"] is True
    assert fetched["result"]["session"]["id"] == session["id"]

    listed = server.handle_line(_req("c3", "session.list", {"kind": "CHAT"}))
    assert listed is not None and listed["ok"] is True
    assert session["id"] in {s["id"] for s in listed["result"]["sessions"]}


def test_session_open_resume(server, tmp_path) -> None:
    created = server.handle_line(_req("o0", "session.create", {"cwd": str(tmp_path), "chat": True}))
    assert created is not None and created["ok"] is True
    session_id = created["result"]["session"]["id"]
    opened = server.handle_line(_req("o1", "session.open", {"ref": session_id}))
    assert opened is not None and opened["ok"] is True
    assert opened["result"]["session"]["id"] == session_id
    assert opened["result"]["created"] is False


def test_session_get_missing_maps_to_not_found(server) -> None:
    response = server.handle_line(_req("m1", "session.get", {"ref": "ses_missing"}))
    assert response is not None and response["ok"] is False
    assert response["error"]["code"] == "NOT_FOUND"


def test_session_create_validates_params(server) -> None:
    bad_cwd = server.handle_line(_req("v1", "session.create", {"cwd": "/no/such/dir/xyz"}))
    assert bad_cwd is not None and bad_cwd["ok"] is False
    assert bad_cwd["error"]["code"] == "INVALID_PARAMS"
    bad_kind = server.handle_line(_req("v2", "session.list", {"kind": "WRONG"}))
    assert bad_kind is not None and bad_kind["ok"] is False
    assert bad_kind["error"]["code"] == "INVALID_PARAMS"


# -- snapshot ---------------------------------------------------------------


def test_snapshot_rebuilds_ui_state(server, tmp_path) -> None:
    created = server.handle_line(_req("s0", "session.create", {"cwd": str(tmp_path), "chat": True}))
    assert created is not None and created["ok"] is True
    session_id = created["result"]["session"]["id"]
    response = server.handle_line(_req("s1", "runtime.snapshot.get"))
    assert response is not None and response["ok"] is True
    snapshot = response["result"]["snapshot"]
    assert snapshot["protocol_version"] == 1
    assert session_id in {s["id"] for s in snapshot["sessions"]}
    assert snapshot["providers"]
    provider = snapshot["providers"][0]
    assert provider["alias"] == "fake"
    assert provider["has_credential"] is True
    blob = json.dumps(snapshot)
    assert "dummy-secret-not-real" not in blob


# -- stdio transport --------------------------------------------------------


def test_stdio_end_to_end(server, tmp_path) -> None:
    stdin = io.StringIO(
        _req("t1", "engine.info")
        + "\n"
        + _req("t2", "session.create", {"cwd": str(tmp_path), "chat": True})
        + "\n{broken\n"
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = run_stdio(server, stdin=stdin, stdout=stdout, stderr=stderr)
    assert code == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["type"] == "hello"
    assert lines[1]["id"] == "t1" and lines[1]["ok"] is True
    assert lines[2]["id"] == "t2" and lines[2]["ok"] is True
    assert lines[3]["ok"] is False and lines[3]["error"]["code"] == "BROKEN_FRAME"
    assert stderr.getvalue() == ""


def test_stdio_overrides_windows_ansi_locale(server, tmp_path, monkeypatch) -> None:
    import sys

    title = "Nueva conversación — diseño 日本語"
    request = json.dumps(
        {
            "id": "unicode",
            "method": "session.create",
            "params": {"cwd": str(tmp_path), "chat": True, "title": title},
        },
        ensure_ascii=False,
    )
    inp = io.TextIOWrapper(io.BytesIO((request + "\n").encode("utf-8")), encoding="cp1252")
    output = io.BytesIO()
    out = io.TextIOWrapper(output, encoding="cp1252")
    with monkeypatch.context() as context:
        context.setattr(sys, "stdin", inp)
        context.setattr(sys, "stdout", out)
        assert run_stdio(server, stderr=io.StringIO()) == 0
        out.flush()
        frames = [json.loads(line) for line in output.getvalue().decode("utf-8").splitlines()]
    response = next(frame for frame in frames if frame.get("id") == "unicode")
    assert response["ok"], response
    assert response["result"]["session"]["title"] == title
