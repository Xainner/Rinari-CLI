"""Turning a provider off and on (plan 0.4.0 #97)."""

from __future__ import annotations

import json

import httpx
import pytest

from rinari.application.model_service import ModelService
from rinari.application.provider_service import AddProviderInput, ProviderService
from rinari.application.services import build_services
from rinari.engine_protocol.server import EngineServer
from rinari.models.router import ModelRouter
from rinari.models.types import ChatMessage, ModelRequest
from rinari.shared.errors import ProviderDisabledError


def _add(service: ProviderService, alias: str, provider_type: str = "openai"):
    return service.add(
        AddProviderInput(alias=alias, provider_type=provider_type, secret="dummy-not-real")
    )


@pytest.fixture
def providers(app_ctx) -> ProviderService:
    return ProviderService(app_ctx)


@pytest.fixture
def models(app_ctx, providers) -> ModelService:
    return ModelService(app_ctx, providers)


def test_every_saved_provider_starts_on(app_ctx, providers) -> None:
    record = _add(providers, "main")
    assert record.enabled is True
    assert providers.get("main").enabled is True


def test_off_hides_its_models_and_include_disabled_brings_them(providers, models) -> None:
    _add(providers, "main")
    _add(providers, "spare", "anthropic")
    models.add("main", "gpt-1", "gpt-main")
    models.add("spare", "claude-1", "claude-spare")

    providers.set_enabled("spare", False)
    assert [m.alias for m in models.list(include_disabled=False)] == ["gpt-main"]
    assert models.list("spare", include_disabled=False) == []
    assert {m.alias for m in models.list()} == {"gpt-main", "claude-spare"}
    assert [p.alias for p in providers.list(include_disabled=False)] == ["main"]

    providers.set_enabled("spare", True)
    assert {m.alias for m in models.list(include_disabled=False)} == {"gpt-main", "claude-spare"}


def test_the_credential_stays_while_off(providers) -> None:
    record = _add(providers, "main")
    providers.set_enabled("main", False)
    assert providers.credential_ref(providers.get(record.id)) is not None
    assert providers.resolve_secret(providers.get(record.id)) == "dummy-not-real"


def test_off_provider_cannot_be_used_and_the_active_one_goes_away(providers, models) -> None:
    _add(providers, "main")
    models.add("main", "gpt-1", "gpt-main")
    providers.use("main")
    providers.set_enabled("main", False)
    assert providers.current() is None
    with pytest.raises(ProviderDisabledError):
        providers.use("main")
    with pytest.raises(ProviderDisabledError):
        models.use("gpt-main")


def test_the_router_refuses_an_off_provider_before_any_call(providers, models) -> None:
    calls: list[httpx.Request] = []
    record = _add(providers, "main")
    model = models.add("main", "gpt-1", "gpt-main")
    providers.set_enabled("main", False)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500))
    )
    router = ModelRouter(providers, models, http_client=client)
    request = ModelRequest(model="gpt-1", messages=(ChatMessage.user("hola"),))
    with pytest.raises(ProviderDisabledError) as excinfo:
        router.invoke(providers.get(record.id), model.id, request)
    assert excinfo.value.machine_code == "PROVIDER_DISABLED"
    assert excinfo.value.details == {
        "provider_id": record.id,
        "provider_alias": "main",
        "model": "gpt-main",
    }
    assert calls == []


def test_refresh_and_discovery_skip_an_off_provider(providers, models) -> None:
    _add(providers, "main")
    models.add("main", "gpt-1", "gpt-main")
    providers.set_enabled("main", False)
    results = models.refresh()
    assert results["main"].error == "disabled"
    assert models.resolve("gpt-main").availability != "unavailable"
    with pytest.raises(ProviderDisabledError):
        models.available("main")


def test_usage_is_not_read_while_off(providers) -> None:
    from rinari.providers.usage import ProviderUsageService

    providers.add(
        AddProviderInput(
            alias="go",
            provider_type="custom",
            endpoint="https://opencode.ai/zen/go/v1",
            secret="dummy-not-real",
            settings={"product_id": "opencode-go"},
        )
    )
    providers.set_enabled("go", False)
    snapshot = ProviderUsageService(providers).get("go")
    assert snapshot["status"] == "disabled"


def test_the_fallback_role_skips_an_off_provider(app_ctx, providers, models) -> None:
    from rinari.models.roles import role_caller, set_roles

    _add(providers, "main")
    _add(providers, "spare", "anthropic")
    models.add("main", "gpt-1", "gpt-main")
    spare = models.add("spare", "claude-1", "claude-spare")
    set_roles(app_ctx, models, fallback=spare.id)
    router = ModelRouter(providers, models)
    assert role_caller(app_ctx, router, "fallback") is not None
    providers.set_enabled("spare", False)
    assert role_caller(app_ctx, router, "fallback") is None


def test_the_protocol_turns_it_off_and_model_list_hides_it(app_ctx, tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    services = build_services(app_ctx, user_home=home)
    _add(services.providers, "main")
    _add(services.providers, "spare", "anthropic")
    services.models.add("main", "gpt-1", "gpt-main")
    services.models.add("spare", "claude-1", "claude-spare")
    server = EngineServer(services, user_home=home)
    try:
        seq = iter(range(100))

        def call(method, params):
            line = {"id": f"r{next(seq)}", "method": method, "params": params}
            response = server.handle_line(json.dumps(line))
            assert response is not None and response["ok"] is True, response
            return response["result"]

        provider = call("provider.update", {"ref": "spare", "enabled": False})["provider"]
        assert provider["enabled"] is False
        listed = call("model.list", {})["models"]
        assert [m["alias"] for m in listed] == ["gpt-main"]
        everything = call("model.list", {"include_disabled": True})["models"]
        assert {m["alias"] for m in everything} == {"gpt-main", "claude-spare"}
        views = {p["alias"]: p for p in call("provider.list", {})["providers"]}
        assert views["spare"]["enabled"] is False and views["main"]["enabled"] is True
        call("provider.update", {"ref": "spare", "enabled": True})
        assert len(call("model.list", {})["models"]) == 2
    finally:
        server.close()
