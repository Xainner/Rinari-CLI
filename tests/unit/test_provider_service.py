import httpx
import pytest

from rinari.application.model_service import ModelService
from rinari.application.provider_service import AddProviderInput, ProviderService
from rinari.shared.errors import (
    ConflictError,
    InvalidUsageError,
    NotFoundError,
)


def _openai(service: ProviderService, alias: str = "openai-personal", secret: str = "sk-a"):
    return service.add(
        AddProviderInput(alias=alias, provider_type="openai", auth_method="api-key", secret=secret)
    )


def _anthropic(service: ProviderService, alias: str = "anthropic-work", secret: str = "sk-b"):
    return service.add(
        AddProviderInput(
            alias=alias, provider_type="anthropic", auth_method="api-key", secret=secret
        )
    )


@pytest.fixture
def providers(app_ctx) -> ProviderService:
    return ProviderService(app_ctx)


@pytest.fixture
def models(app_ctx, providers) -> ModelService:
    return ModelService(app_ctx, providers)


def test_add_first_provider_becomes_active(app_ctx, providers) -> None:
    _openai(providers)
    selection = providers.current()
    assert selection is not None
    assert selection.provider.alias == "openai-personal"
    assert selection.model is None


def test_add_requires_credential(app_ctx, providers) -> None:
    with pytest.raises(InvalidUsageError):
        providers.add(AddProviderInput(alias="x", provider_type="openai"))


def test_add_no_auth_allowed(app_ctx, providers) -> None:
    record = providers.add(
        AddProviderInput(
            alias="local-ollama",
            provider_type="custom",
            auth_method="none",
            endpoint="http://127.0.0.1:11434/v1",
        )
    )
    assert providers.credential_ref(record) is None


def test_add_duplicate_alias_conflict(app_ctx, providers) -> None:
    _openai(providers)
    with pytest.raises(ConflictError):
        _openai(providers)


def test_add_keeps_previous_providers(app_ctx, providers) -> None:
    _openai(providers)
    _anthropic(providers)
    assert [p.alias for p in providers.list()] == ["anthropic-work", "openai-personal"] or (
        len(providers.list()) == 2
    )


def test_use_switches_and_restores_without_deleting(app_ctx, providers, models) -> None:
    _openai(providers)
    _anthropic(providers)
    models.add("openai-personal", "gpt-1", "gpt-main")
    models.add("anthropic-work", "claude-opus-1", "opus")

    providers.use("anthropic-work")
    active = providers.current()
    assert active.provider.alias == "anthropic-work"
    assert active.model.alias == "opus"

    providers.use("openai-personal")
    active = providers.current()
    assert active.provider.alias == "openai-personal"
    assert active.model.alias == "gpt-main"

    assert len(providers.list()) == 2


def test_use_unknown_provider_not_found(app_ctx, providers) -> None:
    with pytest.raises(NotFoundError):
        providers.use("ghost")


def test_current_falls_back_to_provider_default_model(app_ctx, providers, models) -> None:
    # `models add` sets the provider's default model; resolution order
    # (commands.md #20) must use it even without an explicit `model use`.
    _openai(providers)
    models.add("openai-personal", "gpt-1", "gpt-main")
    active = providers.current()
    assert active is not None
    assert active.model is not None
    assert active.model.alias == "gpt-main"


def test_explicit_model_use_wins_over_default_fallback(app_ctx, providers, models) -> None:
    _openai(providers)
    first = models.add("openai-personal", "gpt-1", "gpt-main")
    second = models.add("openai-personal", "gpt-2", "gpt-next")
    models.use(second.id, "openai-personal")
    assert providers.current().model.alias == "gpt-next"
    models.use(first.id, "openai-personal")
    assert providers.current().model.alias == "gpt-main"


def test_current_fallback_never_pairs_foreign_model(app_ctx, providers, models) -> None:
    _openai(providers)
    _anthropic(providers)
    models.add("anthropic-work", "claude-opus-1", "opus")
    providers.use("openai-personal")
    active = providers.current()
    assert active.provider.alias == "openai-personal"
    assert active.model is None


def test_logout_preserves_provider_models_and_clears_credential(app_ctx, providers, models) -> None:
    _openai(providers)
    models.add("openai-personal", "gpt-1", "gpt-main")
    record = providers.logout("openai-personal")

    assert providers.get("openai-personal").id == record.id
    assert len(models.list()) == 1
    assert record.status_connected is False
    assert providers.credential_ref(providers.get("openai-personal")) is None


def test_remove_inactive_provider_only(app_ctx, providers, models) -> None:
    _openai(providers)
    a = _anthropic(providers)
    models.add("anthropic-work", "claude-opus-1", "opus")

    providers.remove(a.alias)

    assert [p.alias for p in providers.list()] == ["openai-personal"]
    assert models.list() == []
    assert providers.current().provider.alias == "openai-personal"


def test_remove_active_provider_requires_switch(app_ctx, providers) -> None:
    _openai(providers)
    with pytest.raises(InvalidUsageError):
        providers.remove("openai-personal")


def test_remove_active_provider_with_switch(app_ctx, providers, models) -> None:
    _openai(providers)
    _anthropic(providers)
    models.add("openai-personal", "gpt-1", "gpt-main")
    models.add("anthropic-work", "claude-opus-1", "opus")

    providers.remove("openai-personal", switch_to="anthropic-work")

    active = providers.current()
    assert active.provider.alias == "anthropic-work"
    assert active.model.alias == "opus"


def test_rename_preserves_id_and_updates_alias(app_ctx, providers) -> None:
    record = _openai(providers)
    renamed = providers.rename("openai-personal", "openai-main")
    assert renamed.id == record.id
    assert providers.get("openai-main").id == record.id
    with pytest.raises(NotFoundError):
        providers.get("openai-personal")


def test_rename_conflict(app_ctx, providers) -> None:
    _openai(providers)
    _anthropic(providers)
    with pytest.raises(ConflictError):
        providers.rename("openai-personal", "anthropic-work")


def test_login_unsupported_by_builtin_adapters(app_ctx, providers) -> None:
    _openai(providers)
    with pytest.raises(InvalidUsageError) as excinfo:
        providers.login("openai-personal")
    assert "api-key" in (excinfo.value.hint or "")


def test_test_updates_connection_status(app_ctx, providers) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "gpt-1"}]})

    service = ProviderService(
        app_ctx, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    _openai(service)
    health = service.test("openai-personal")
    assert health.connected is True
    assert health.models_discovered == 1
    assert service.get("openai-personal").status_connected is True


def test_test_auth_failure_reports_disconnected(app_ctx, providers) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    service = ProviderService(
        app_ctx, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    _openai(service)
    health = service.test("openai-personal")
    assert health.connected is False
    assert service.get("openai-personal").status_connected is False


def test_discover_reports_env_creds_without_saving(app_ctx, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-secret")
    service = ProviderService(
        app_ctx, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    candidates = service.discover()
    assert any(c.source == "environment" and c.name == "OPENAI_API_KEY" for c in candidates)
    assert service.list() == []


def test_secret_never_outside_credentials_dir(app_ctx, providers) -> None:
    secret = "sk-leakcheck-0123456789"
    _openai(providers, secret=secret)
    for path in app_ctx.home.rglob("*"):
        if path.is_file() and "credentials" not in path.parts:
            assert secret not in path.read_text(encoding="utf-8", errors="ignore"), str(path)


def test_env_ref_secret_resolution(app_ctx, providers, monkeypatch) -> None:
    monkeypatch.setenv("RINARI_TEST_KEY", "sk-from-env")
    record = providers.add(
        AddProviderInput(
            alias="env-provider",
            provider_type="anthropic",
            auth_method="api-key",
            secret_env="RINARI_TEST_KEY",
        )
    )
    assert providers.resolve_secret(record) == "sk-from-env"
    assert providers.credential_ref(record) == "env://RINARI_TEST_KEY"
