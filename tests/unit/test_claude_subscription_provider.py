"""Claude Subscription as a provider: identity, credential contract, diagnostics.

These are the invariants that keep a subscription from quietly becoming API
billing, and Rinari from ever holding a Claude credential (plan sections 5,
27, 28, 60, 66).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.server import EngineServer
from rinari.providers.catalog import CLAUDE_CLI_ENDPOINT, catalog_view, product_for
from rinari.providers.registry import adapter_for
from rinari.shared.errors import InvalidUsageError, ProviderModelError
from rinari.storage.records import ProviderRecord

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_claude.py"
WINDOWS = os.name == "nt"
SETTINGS = {"product_id": "claude-subscription", "transport": "claude-cli"}


@pytest.fixture
def services(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    return build_services(app_ctx, user_home=user_home)


@pytest.fixture
def engine_server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


def fake_cli(tmp_path: Path) -> Path:
    if WINDOWS:
        path = tmp_path / "claude.cmd"
        path.write_text(f'@echo off\r\n"{sys.executable}" "{FIXTURE}" %*\r\n', encoding="utf-8")
        return path
    path = tmp_path / "claude"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURE}" "$@"\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def record(**overrides) -> ProviderRecord:
    base = {
        "id": "prov_test",
        "alias": "claude-sub",
        "type": "custom",
        "auth_method": "external-cli",
        "account_hint": None,
        "endpoint": CLAUDE_CLI_ENDPOINT,
        "settings": dict(SETTINGS),
        "default_model_id": None,
        "last_used_model_id": None,
        "status_connected": None,
        "status_checked_at": None,
        "created_at": "2026-09-30T00:00:00Z",
        "updated_at": "2026-09-30T00:00:00Z",
    }
    base.update(overrides)
    return ProviderRecord(**base)


# -- product identity ------------------------------------------------------


def test_the_product_needs_endpoint_auth_and_transport_to_agree():
    assert product_for(record()) == "claude-subscription"


@pytest.mark.parametrize(
    "overrides",
    [
        {"auth_method": "api-key"},
        {"endpoint": "https://api.anthropic.com/v1"},
        {"settings": {"product_id": "claude-subscription"}},
        {"settings": {"product_id": "claude-subscription", "transport": "http"}},
    ],
)
def test_a_custom_provider_cannot_claim_the_product_by_itself(overrides):
    """`product_id` alone must not buy credential-free, process-spawning behaviour."""
    assert product_for(record(**overrides)) != "claude-subscription"


def test_the_catalog_tells_the_desktop_it_needs_a_binary_not_a_key():
    entry = next(e for e in catalog_view() if e["id"] == "claude-subscription")
    assert entry["auth_methods"] == ["external-cli"]
    assert entry["runtime"] == "claude-cli"
    assert entry["requires_external_binary"] == "claude"
    assert entry["experimental"] is True


def test_an_unknown_external_cli_product_is_refused():
    with pytest.raises(InvalidUsageError):
        adapter_for(record(endpoint="process://something-else", settings={}))


# -- credential contract ---------------------------------------------------


def test_creating_it_stores_no_credential(services, tmp_path, monkeypatch):
    monkeypatch.setenv("RINARI_CLAUDE_COMMAND", str(fake_cli(tmp_path)))
    saved = services.providers.add(
        AddProviderInput(
            alias="claude-sub",
            provider_type="custom",
            auth_method="external-cli",
            endpoint=CLAUDE_CLI_ENDPOINT,
            settings=dict(SETTINGS),
        )
    )
    assert services.providers.credential_ref(saved) is None
    assert services.providers.resolve_secret(saved) is None


def test_it_refuses_to_take_a_credential(services, tmp_path, monkeypatch):
    monkeypatch.setenv("RINARI_CLAUDE_COMMAND", str(fake_cli(tmp_path)))
    with pytest.raises(InvalidUsageError):
        services.providers.add(
            AddProviderInput(
                alias="claude-sub",
                provider_type="custom",
                auth_method="external-cli",
                endpoint=CLAUDE_CLI_ENDPOINT,
                secret="sk-nope",
                settings=dict(SETTINGS),
            )
        )


@pytest.mark.parametrize("auth", ["logged_out", "console", "bedrock"])
def test_it_is_not_saved_when_the_cli_is_not_on_a_subscription(
    services, tmp_path, monkeypatch, auth
):
    """A saved provider that looks connected is how API billing sneaks in."""
    monkeypatch.setenv("RINARI_CLAUDE_COMMAND", str(fake_cli(tmp_path)))
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", auth)
    with pytest.raises(ProviderModelError):
        services.providers.add(
            AddProviderInput(
                alias="claude-sub",
                provider_type="custom",
                auth_method="external-cli",
                endpoint=CLAUDE_CLI_ENDPOINT,
                settings=dict(SETTINGS),
            )
        )
    assert services.providers.list() == []


# -- protocol surface ------------------------------------------------------


def test_the_engine_announces_the_capabilities(engine_server):
    capabilities = engine_server.hello()["capabilities"]
    assert capabilities["provider_external_cli_v1"] is True
    assert capabilities["claude_subscription_v1"] is True


def test_diagnostics_report_the_runtime_without_leaking_anything(
    engine_server, services, tmp_path, monkeypatch
):
    binary = fake_cli(tmp_path)
    monkeypatch.setenv("RINARI_CLAUDE_COMMAND", str(binary))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    saved = services.providers.add(
        AddProviderInput(
            alias="claude-sub",
            provider_type="custom",
            auth_method="external-cli",
            endpoint=CLAUDE_CLI_ENDPOINT,
            settings=dict(SETTINGS),
        )
    )
    response = engine_server.handle_line(
        json.dumps({"id": "d1", "method": "provider.diagnostics.get", "params": {"ref": saved.id}})
    )
    assert response["ok"] is True
    runtime = response["result"]["runtime"]
    assert runtime["installed"] is True
    assert runtime["state"] == "connected"
    assert runtime["supported"] is True
    assert runtime["auth"]["safe_for_subscription"] is True
    # It says which billing overrides it strips, which is the point...
    assert "ANTHROPIC_API_KEY" in runtime["sanitized_env"]
    # ...without ever carrying their values, or anything else sensitive.
    blob = json.dumps(response["result"])
    assert "sk-secret" not in blob
    assert ".claude" not in blob


def test_the_setup_flow_can_probe_the_runtime_before_a_provider_exists(
    engine_server, tmp_path, monkeypatch
):
    """The card must tell "not installed" from "wrong account" before saving."""
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "console")
    monkeypatch.setenv("RINARI_CLAUDE_COMMAND", str(fake_cli(tmp_path)))
    response = engine_server.handle_line(
        json.dumps(
            {
                "id": "p1",
                "method": "provider.runtime.probe",
                "params": {"runtime": "claude-cli"},
            }
        )
    )
    assert response["ok"] is True
    runtime = response["result"]["runtime"]
    assert runtime["installed"] is True
    assert runtime["state"] == "non_subscription_auth"
    assert runtime["auth"]["safe_for_subscription"] is False
    # The card shows this to paste: the CLI here is found via the env
    # override, not PATH, so the command names it by full path.
    assert runtime["login_command"].endswith("auth login --claudeai")
    assert str(fake_cli(tmp_path)) in runtime["login_command"]


def test_probing_an_unknown_runtime_is_rejected(engine_server):
    response = engine_server.handle_line(
        json.dumps({"id": "p2", "method": "provider.runtime.probe", "params": {"runtime": "nope"}})
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_PARAMS"


def test_the_probe_is_declared_in_the_protocol_schema():
    import json as _json

    schema = _json.loads(
        (
            Path(__file__).resolve().parents[2] / "src/rinari/engine_protocol/schema/v1.json"
        ).read_text(encoding="utf-8")
    )
    methods = schema["$defs"]["method"]["enum"]
    assert "provider.runtime.probe" in methods


# -- enrutado del transporte externo ---------------------------------------


def _saved_model(services, tmp_path, monkeypatch, alias="sonnet"):
    monkeypatch.setenv("RINARI_CLAUDE_COMMAND", str(fake_cli(tmp_path)))
    provider = services.providers.add(
        AddProviderInput(
            alias="claude-sub",
            provider_type="custom",
            auth_method="external-cli",
            endpoint=CLAUDE_CLI_ENDPOINT,
            settings=dict(SETTINGS),
        )
    )
    return provider, services.models.add(provider.id, alias, alias)


def test_the_router_routes_an_external_runtime_instead_of_rejecting_it(
    services, tmp_path, monkeypatch
):
    """Declaring the product's transport must not make its turns unroutable.

    `claude-cli` is a real route owned by the adapter, but the router's guards
    only knew the HTTP wire transports: one flattened every capability to
    false (the composer greyed out the whole effort selector) and the other
    refused the call outright with "a transport not yet implemented". Both
    were found by the owner, one after the other, on a real turn.
    """
    from rinari.models.router import ModelRouter
    from rinari.models.types import ChatMessage, ModelRequest

    provider, model = _saved_model(services, tmp_path, monkeypatch)
    router = ModelRouter(services.providers, services.models)

    caps = router.capabilities(provider, model.id)
    assert caps.reasoning_effort is True
    assert caps.streaming is True
    matrix = router.capability_matrix(provider, model.id)
    assert matrix["capabilities"]["reasoning_levels"] == ["low", "medium", "high", "xhigh", "max"]
    assert matrix["metadata"]["route_supported"] is True

    response = router.invoke(
        provider, model.id, ModelRequest(model=model.alias, messages=(ChatMessage.user("hola"),))
    )
    assert response.content


def test_a_level_the_cli_rejects_is_dropped_with_a_notice_not_sent(services, tmp_path, monkeypatch):
    """An effort the model does not take is a preference, not the task.

    Main drops it and says so (`provider.reasoning.dropped`) instead of ending
    the turn; for this transport that also means `--effort` never carries a
    level the CLI would silently ignore.
    """
    from rinari.models.router import ModelRouter
    from rinari.models.types import ChatMessage, ModelRequest

    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    provider, model = _saved_model(services, tmp_path, monkeypatch)
    router = ModelRouter(services.providers, services.models)
    notices: list[str] = []
    request = ModelRequest(
        model=model.alias,
        messages=(ChatMessage.user("hola"),),
        reasoning_effort="ultra",
        usage_observer=lambda kind, _payload: notices.append(kind),
    )
    router.invoke(provider, model.id, request)
    assert "provider.reasoning.dropped" in notices
    argv = json.loads(record.read_text(encoding="utf-8"))["argv"]
    assert "--effort" not in argv


# -- auth del proveedor (seccion 26) ---------------------------------------


def _rpc(engine_server, method, params):
    return engine_server.handle_line(json.dumps({"id": method, "method": method, "params": params}))


@pytest.mark.parametrize(
    ("auth", "expected"),
    [("subscription", "connected"), ("logged_out", "needs_auth"), ("console", "error")],
)
def test_auth_get_reads_the_cli_instead_of_the_oauth_store(
    engine_server, services, tmp_path, monkeypatch, auth, expected
):
    """The generic OAuth service answered "disconnected" for a working provider."""
    provider, _ = _saved_model(services, tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", auth)
    response = _rpc(engine_server, "provider.auth.get", {"ref": provider.id})
    assert response["ok"] is True
    snapshot = response["result"]
    assert snapshot["status"] == expected
    assert snapshot["auth_kind"] == "external-cli"
    assert snapshot["managed_by"] == "claude-cli"
    assert snapshot["authorization_url"] is None


def test_auth_logout_never_signs_claude_code_out(engine_server, services, tmp_path, monkeypatch):
    """`claude auth logout` would sign the user out of Claude Code everywhere."""
    provider, _ = _saved_model(services, tmp_path, monkeypatch)
    record = tmp_path / "logout.json"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    response = _rpc(engine_server, "provider.auth.logout", {"ref": provider.id})
    assert response["ok"] is False
    assert not record.exists()
    # The provider is still there: disconnecting from Rinari is removing it.
    assert services.providers.get(provider.id).id == provider.id


def test_auth_start_points_at_the_cli_instead_of_faking_a_login(
    engine_server, services, tmp_path, monkeypatch
):
    provider, _ = _saved_model(services, tmp_path, monkeypatch)
    response = _rpc(engine_server, "provider.auth.start", {"ref": provider.id, "method": "browser"})
    assert response["ok"] is False
    assert "claude auth login" in json.dumps(response["error"])


# -- nunca ejecutar un binario ajeno ----------------------------------------


def _impostor(tmp_path: Path) -> tuple[Path, Path]:
    """A program that is not the Claude CLI and leaves a mark if it runs."""
    marker = tmp_path / "IMPOSTOR_RAN"
    if WINDOWS:
        path = tmp_path / "evil.cmd"
        path.write_text(f'@echo off\r\necho ran> "{marker}"\r\n', encoding="utf-8")
    else:
        path = tmp_path / "evil"
        path.write_text(f'#!/bin/sh\necho ran > "{marker}"\n', encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path, marker


def test_the_probe_does_not_take_a_path_from_the_desktop(engine_server, tmp_path, monkeypatch):
    """A probe that took a path let the renderer run any program on disk."""
    impostor, marker = _impostor(tmp_path)
    monkeypatch.setenv("RINARI_CLAUDE_COMMAND", str(fake_cli(tmp_path)))
    _rpc(
        engine_server,
        "provider.runtime.probe",
        {"runtime": "claude-cli", "command_path": str(impostor)},
    )
    assert not marker.exists()


def test_a_saved_override_that_is_not_the_cli_is_refused(services, tmp_path, monkeypatch):
    impostor, marker = _impostor(tmp_path)
    monkeypatch.setenv("RINARI_CLAUDE_COMMAND", str(fake_cli(tmp_path)))
    with pytest.raises(InvalidUsageError):
        services.providers.add(
            AddProviderInput(
                alias="claude-sub",
                provider_type="custom",
                auth_method="external-cli",
                endpoint=CLAUDE_CLI_ENDPOINT,
                settings={**SETTINGS, "command_path": str(impostor)},
            )
        )
    assert not marker.exists()
    assert services.providers.list() == []


def test_an_override_changed_later_is_still_never_executed(tmp_path):
    """Settings can change after creation; the registry guards that path."""
    impostor, marker = _impostor(tmp_path)
    adapter = adapter_for(record(settings={**SETTINGS, "command_path": str(impostor)}))
    adapter.runtime.auth_status()
    assert not marker.exists()
    found = adapter.runtime.resolve()
    assert found is None or Path(found.path) != impostor
