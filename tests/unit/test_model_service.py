import httpx
import pytest

from rinari.application.model_service import ModelService
from rinari.application.provider_service import AddProviderInput, ProviderService
from rinari.shared.errors import ConflictError, InvalidUsageError, NotFoundError


def _openai(service: ProviderService, alias: str = "openai-personal") -> None:
    service.add(
        AddProviderInput(alias=alias, provider_type="openai", auth_method="api-key", secret="sk-a")
    )


def _anthropic(service: ProviderService, alias: str = "anthropic-work") -> None:
    service.add(
        AddProviderInput(
            alias=alias, provider_type="anthropic", auth_method="api-key", secret="sk-b"
        )
    )


@pytest.fixture
def providers(app_ctx) -> ProviderService:
    return ProviderService(app_ctx)


@pytest.fixture
def models(app_ctx, providers) -> ModelService:
    return ModelService(app_ctx, providers)


def test_add_sets_provider_default_when_first(models) -> None:
    _openai(models._providers)
    record = models.add("openai-personal", "gpt-1", "gpt-main")
    provider = models._providers.get("openai-personal")
    assert provider.default_model_id == record.id


def test_add_duplicate_model_id_conflict(models) -> None:
    _openai(models._providers)
    models.add("openai-personal", "gpt-1", "gpt-main")
    with pytest.raises(ConflictError):
        models.add("openai-personal", "gpt-1", "other-alias")


def test_alias_rename_preserves_id(models) -> None:
    _openai(models._providers)
    record = models.add("openai-personal", "gpt-1", "gpt-main")
    renamed = models.alias("gpt-main", "main")
    assert renamed.id == record.id
    assert models.resolve("main").id == record.id


def test_remove_clears_provider_default(models) -> None:
    _openai(models._providers)
    record = models.add("openai-personal", "gpt-1", "gpt-main")
    models.remove(record.alias)
    assert models._providers.get("openai-personal").default_model_id is None
    assert models.list() == []


def test_remove_active_model_clears_active_selection(app_ctx, models) -> None:
    _openai(models._providers)
    record = models.add("openai-personal", "gpt-1", "gpt-main")
    models.use("gpt-main")
    models.remove(record.alias)
    assert app_ctx.config_repo.get("active_model") is None
    assert app_ctx.config_repo.get("active_provider") is not None


def test_resolve_by_alias_and_provider_model_id(models) -> None:
    _openai(models._providers)
    record = models.add("openai-personal", "gpt-1", "gpt-main")
    assert models.resolve("gpt-main").id == record.id
    assert models.resolve("gpt-1").id == record.id
    assert models.resolve(record.id).id == record.id


def test_resolve_ambiguous_across_providers(models) -> None:
    _openai(models._providers)
    _anthropic(models._providers)
    models.add("openai-personal", "shared-1", "shared")
    models.add("anthropic-work", "shared-1", "shared")
    with pytest.raises(InvalidUsageError):
        models.resolve("shared")
    assert models.resolve("shared", "anthropic-work").provider_id.startswith("prov_")


def test_resolve_unknown_not_found(models) -> None:
    _openai(models._providers)
    with pytest.raises(NotFoundError):
        models.resolve("does-not-exist")


def test_model_switch_preserves_both_models(app_ctx, models) -> None:
    _openai(models._providers)
    a1 = models.add("openai-personal", "gpt-1", "gpt-main", capabilities={"reasoning": True})
    models.add("openai-personal", "gpt-2", "gpt-fast")

    models.use("gpt-main")
    models.use("gpt-fast")
    models.use("gpt-main")

    listing = {m.alias: m for m in models.list()}
    assert set(listing) == {"gpt-main", "gpt-fast"}
    assert listing["gpt-main"].capabilities == {"reasoning": True}
    assert app_ctx.config_repo.get("active_model") == a1.id


def test_model_use_from_other_provider_switches_provider(models) -> None:
    _openai(models._providers)
    _anthropic(models._providers)
    models.add("openai-personal", "gpt-1", "gpt-main")
    models.add("anthropic-work", "claude-opus-1", "opus")

    resolved = models.use("opus")
    assert resolved.switched_provider is True
    assert resolved.provider.alias == "anthropic-work"
    current = models._providers.current()
    assert current.provider.alias == "anthropic-work"
    assert current.model.alias == "opus"


def test_reset_falls_back_to_provider_default(models) -> None:
    _openai(models._providers)
    a1 = models.add("openai-personal", "gpt-1", "gpt-main")
    models.add("openai-personal", "gpt-2", "gpt-fast")
    models.use("gpt-fast")
    models.set_provider_default("openai-personal", "gpt-main")
    resolved = models.reset("openai-personal")
    assert resolved is not None
    assert resolved.model.id == a1.id


def test_refresh_marks_missing_models_unavailable_without_deleting(models) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-1"}]})
        return httpx.Response(404)

    service = ModelService(
        models._ctx,
        ProviderService(
            models._ctx, http_client=httpx.Client(transport=httpx.MockTransport(handler))
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    _openai(service._providers)
    models.add("openai-personal", "gpt-1", "gpt-main")
    models.add("openai-personal", "gpt-2", "gpt-gone")

    result = service.refresh("openai-personal")["openai-personal"]
    assert result.saved == 2
    assert result.still_available == 1
    assert result.marked_unavailable == 1

    listing = {m.alias: m for m in models.list()}
    assert listing["gpt-main"].availability == "available"
    assert listing["gpt-gone"].availability == "unavailable"


def test_refresh_reports_error_when_provider_unreachable(models) -> None:
    _openai(models._providers)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    service = ModelService(
        models._ctx,
        ProviderService(
            models._ctx, http_client=httpx.Client(transport=httpx.MockTransport(handler))
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    models.add("openai-personal", "gpt-1", "gpt-main")
    result = service.refresh("openai-personal")["openai-personal"]
    assert result.error is not None
    # availability is left untouched when discovery fails
    assert models.list()[0].availability == "unknown"


def test_model_test_present_in_catalog(models) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "gpt-1"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = ModelService(
        models._ctx, ProviderService(models._ctx, http_client=client), http_client=client
    )
    _openai(service._providers)
    models.add("openai-personal", "gpt-1", "gpt-main")
    result = service.test("gpt-main")
    assert result.ok is True


def test_model_test_missing_from_catalog(models) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "gpt-1"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = ModelService(
        models._ctx, ProviderService(models._ctx, http_client=client), http_client=client
    )
    _openai(service._providers)
    models.add("openai-personal", "gpt-other", "gpt-other")
    result = service.test("gpt-other")
    assert result.ok is False
