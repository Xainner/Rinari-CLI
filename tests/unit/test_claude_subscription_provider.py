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
    response = engine_server.handle_line(
        json.dumps(
            {
                "id": "p1",
                "method": "provider.runtime.probe",
                "params": {"runtime": "claude-cli", "command_path": str(fake_cli(tmp_path))},
            }
        )
    )
    assert response["ok"] is True
    runtime = response["result"]["runtime"]
    assert runtime["installed"] is True
    assert runtime["state"] == "non_subscription_auth"
    assert runtime["auth"]["safe_for_subscription"] is False


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
