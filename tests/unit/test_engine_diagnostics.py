"""`engine.diagnostics`: sizes, state and recent failures, never content or secrets."""

from __future__ import annotations

import json

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol import diagnostics
from rinari.engine_protocol.server import EngineServer
from rinari.storage.records import SessionMessageRecord

SECRET = "sk-diagnostic-secret-not-real-000000"
PRIVATE = "https://gateway.internal-company.example/v1"


@pytest.fixture
def services(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    container = build_services(app_ctx, user_home=user_home)
    container.providers.add(
        AddProviderInput(alias="openai-main", provider_type="openai", secret=SECRET)
    )
    container.providers.add(
        AddProviderInput(
            alias="gateway", provider_type="custom", endpoint=PRIVATE, secret="dummy-secret"
        )
    )
    container.providers.add(
        AddProviderInput(
            alias="local",
            provider_type="custom",
            endpoint="http://127.0.0.1:9/v1",
            auth_method="none",
        )
    )
    container.models.add("openai-main", "gpt-x", "main")
    container.providers.use("openai-main")
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


def _report(server):
    response = server.handle_line(_req("d", "engine.diagnostics"))
    assert response is not None and response["ok"] is True
    return response["result"]["diagnostics"]


def test_report_has_versions_sizes_and_largest_sessions(server, services, tmp_path) -> None:
    created = server.handle_line(
        _req("c", "session.create", {"cwd": str(tmp_path), "chat": True, "title": "Plan secreto"})
    )
    session_id = created["result"]["session"]["id"]
    services.ctx.message_repo.append_many(
        session_id,
        [
            SessionMessageRecord(
                id="m1",
                session_id=session_id,
                seq=0,
                role="user",
                content="CONTENIDO PRIVADO " + "x" * 5000,
                created_at="2026-01-01T00:00:00Z",
            )
        ],
    )
    report = _report(server)
    assert report["engine"]["protocol_version"] == 1
    assert report["engine"]["python"]
    storage = report["storage"]
    assert storage["sessions"] >= 1 and storage["messages"] >= 1
    top = storage["largest_sessions"][0]
    assert top["session_id"] == session_id and top["message_bytes"] > 5000
    assert storage["files"]["state_db"] > 0
    blob = json.dumps(report)
    assert "CONTENIDO PRIVADO" not in blob
    assert "Plan secreto" not in blob


def test_providers_are_described_without_credentials_or_private_endpoints(server) -> None:
    report = _report(server)
    providers = {row["type"] + ":" + row["endpoint"]: row for row in report["providers"]}
    assert providers["openai:known"]["product_id"] == "openai"
    assert providers["openai:known"]["active"] is True
    assert providers["openai:known"]["has_credential"] is True
    assert providers["custom:custom"]["product_id"] == "custom"
    assert "custom:local" in providers
    blob = json.dumps(report)
    assert SECRET not in blob
    assert "internal-company" not in blob
    assert "gateway" not in blob  # aliases are the user's names, not needed to diagnose


def test_failed_and_slow_requests_are_recorded_without_params(server, monkeypatch) -> None:
    server.handle_line(_req("x1", "session.history", {"ref": "ses-does-not-exist-PRIVADO"}))
    server.handle_line(_req("x2", "no.such.method"))
    monkeypatch.setattr(diagnostics, "SLOW_REQUEST_MS", 0)
    server.handle_line(_req("x3", "engine.info"))
    recent = _report(server)["recent_requests"]
    failures = {entry["method"]: entry for entry in recent if "code" in entry}
    assert failures["session.history"]["code"] == "NOT_FOUND"
    assert failures["no.such.method"]["code"] == "UNKNOWN_METHOD"
    assert any(entry["method"] == "engine.info" and "code" not in entry for entry in recent)
    assert "PRIVADO" not in json.dumps(recent)


def test_a_broken_section_does_not_hide_the_others(server, services, monkeypatch) -> None:
    def broken(*_args, **_kwargs):
        raise RuntimeError("plugin store unavailable")

    monkeypatch.setattr(services.plugins, "list", broken)
    report = _report(server)
    assert "RuntimeError" in report["extensions"]["error"]
    assert isinstance(report["providers"], list)
    assert report["storage"]["sessions"] >= 0


def test_request_log_keeps_only_the_latest_entries() -> None:
    log = diagnostics.RequestLog()
    for index in range(80):
        log.record(_req(str(index), f"m.{index}"), {"ok": False, "error": {"code": "X"}}, 1)
    entries = log.entries()
    assert len(entries) == 50
    assert entries[-1]["method"] == "m.79"
    log.record(_req("ok", "fast"), {"ok": True}, 1)
    assert log.entries()[-1]["method"] == "m.79"


def test_slow_request_diagnostic_names_the_method_not_its_params(server, monkeypatch) -> None:
    import io

    from rinari.engine_protocol.transports import stdio

    monkeypatch.setattr(stdio, "SLOW_REQUEST_S", -1.0)
    stdin = io.StringIO(_req("s", "session.history", {"ref": "ses-PARAMETRO-PRIVADO"}) + "\n")
    stderr = io.StringIO()
    stdio.run_stdio(server, stdin=stdin, stdout=io.StringIO(), stderr=stderr)
    assert "slow request" in stderr.getvalue()
    assert "session.history" in stderr.getvalue()
    assert "PARAMETRO-PRIVADO" not in stderr.getvalue()
