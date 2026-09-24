"""Effective context capacity: one resolver, honest provenance (review plan §A).

Preflight and ``context.status`` share ``context.settings.window``. These
tests drive it through a saved provider/model and the real router, with only
the network adapter replaced, so every case exercises the same precedence the
engine uses: manual window, explicit override, endpoint, catalog, fallback.
"""

import json
from types import SimpleNamespace

import pytest

from rinari.application.model_service import ModelService
from rinari.application.provider_service import AddProviderInput, ProviderService
from rinari.artifacts.store import ArtifactStore
from rinari.context import settings as context_settings
from rinari.context import windows
from rinari.models.router import ModelRouter
from rinari.models.types import ChatMessage, ModelRequest, ProviderCapabilities
from rinari.providers.adapters.base import DiscoveredModel
from rinari.providers.metadata import catalog
from rinari.runtime.model_caller import ModelCaller

OPENCODE_GO = "https://opencode.ai/zen/go/v1"


class FakeAdapter:
    """Only the network edge is fake: discovery answers what the test says."""

    default_max_tokens = None

    def __init__(self):
        self.models = {}
        self.fail = False
        self.calls = []

    def capabilities(self):
        return ProviderCapabilities()

    def list_models(self, secret, endpoint=None):
        self.calls.append(endpoint)
        if self.fail:
            raise RuntimeError("endpoint offline")
        return [DiscoveredModel(mid, capabilities=caps) for mid, caps in self.models.items()]


@pytest.fixture
def clock(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(windows.time, "time", lambda: now[0])
    return now


@pytest.fixture
def world(app_ctx, monkeypatch, clock):
    adapter = FakeAdapter()
    monkeypatch.setattr(ModelRouter, "adapter", lambda self, provider: adapter)
    # ModelService.refresh builds its own adapter; it has to see the same edge.
    monkeypatch.setattr(
        "rinari.application.model_service.adapter_for", lambda provider, client=None: adapter
    )
    providers = ProviderService(app_ctx)
    models = ModelService(app_ctx, providers)
    router = ModelRouter(providers, models)

    def destination(model_id="model-a", endpoint="https://gateway.example/v1", **add):
        alias = f"p{len(providers.list())}"
        if not any(p.endpoint == endpoint for p in providers.list()):
            providers.add(
                AddProviderInput(
                    alias=alias, provider_type="custom", endpoint=endpoint, secret="sk-test"
                )
            )
        provider = next(p for p in providers.list() if p.endpoint == endpoint)
        record = models.add(provider.alias, model_id, f"{model_id}@{provider.alias}", **add)
        return ModelCaller(router, provider, record.id), record

    return SimpleNamespace(
        ctx=app_ctx, adapter=adapter, providers=providers, models=models, destination=destination
    )


def resolved(world, caller):
    return context_settings.window(world.ctx, caller)


def budget(world, caller, max_tokens=None):
    request = ModelRequest(model="x", messages=(ChatMessage.user("hi"),), max_tokens=max_tokens)
    return context_settings.input_budget(world.ctx, caller, request, resolved(world, caller))


def status(world, model_id):
    handlers = {}
    services = SimpleNamespace(
        ctx=world.ctx,
        models=world.models,
        providers=world.providers,
        artifacts=ArtifactStore(world.ctx),
    )
    from rinari.engine_protocol.media import register_media

    register_media(SimpleNamespace(register=handlers.__setitem__), services)
    return handlers["context.status"]({"model_id": model_id})


def test_endpoint_window_drives_preflight_and_visible_status(world):
    world.adapter.models = {"model-a": {"context_length": 64000}}
    caller, record = world.destination()
    assert resolved(world, caller)["window_tokens"] == 64000
    assert resolved(world, caller)["window_source"] == "provider"
    assert budget(world, caller) == 64000
    visible = status(world, record.id)
    assert visible["window_tokens"] == 64000
    assert visible["window_source"] == "provider"
    assert visible["window_estimated"] is False


def test_output_only_metadata_never_confirms_a_window(world):
    world.adapter.models = {"model-a": {"max_output_tokens": 4096}}
    caller, _ = world.destination()
    result = resolved(world, caller)
    assert result["window_tokens"] == 128_000
    assert result["window_source"] == "fallback"
    assert result["window_estimated"] is True
    assert result["max_output_tokens"] == 4096


def test_catalog_fills_a_known_model_and_says_so(world):
    # OpenCode Go does not announce limits; the bundled models.dev snapshot
    # does. The window comes from the catalog, labelled as such, with its date.
    world.adapter.models = {"glm-5.3-flash": None}
    caller, _ = world.destination("glm-5.3-flash", OPENCODE_GO)
    result = resolved(world, caller)
    assert result["window_tokens"] == 1_000_000
    assert result["window_source"] == "catalog"
    assert result["window_estimated"] is False
    assert result["limit_sources"]["context"] == "catalog"
    assert result["metadata_updated_at"] == catalog()["updated_at"]
    assert result["max_output_tokens"] == 131_072
    # The catalog's maximum output is a limit, not a reservation: nothing in
    # the request or configuration asked for 131k of output.
    assert budget(world, caller) == 1_000_000
    assert budget(world, caller, max_tokens=8192) == 1_000_000 - 8192
    # Vision comes from the same shared metadata, not from another runtime.
    assert caller.capabilities().vision is True


def test_the_endpoint_outranks_the_catalog(world):
    world.adapter.models = {"glm-5.3-flash": {"context_length": 200_000}}
    caller, _ = world.destination("glm-5.3-flash", OPENCODE_GO)
    result = resolved(world, caller)
    assert result["window_tokens"] == 200_000
    assert result["window_source"] == "provider"
    assert result["limit_sources"]["output"] == "catalog"


def test_an_explicit_override_outranks_the_endpoint(world):
    world.adapter.models = {"model-a": {"context_length": 64000}}
    caller, _ = world.destination(capabilities={"max_context_tokens": 32000})
    result = resolved(world, caller)
    assert result["window_tokens"] == 32000
    assert result["window_source"] == "override"


def test_a_manual_window_wins_without_querying_the_endpoint(world):
    world.adapter.models = {"model-a": {"context_length": 64000}}
    caller, record = world.destination()
    policy = world.ctx.home / "context-policy.json"
    policy.write_text(json.dumps({"model_windows": {record.id: 50000}}), encoding="utf-8")
    result = resolved(world, caller)
    assert result["window_tokens"] == 50000
    assert result["window_source"] == "manual"
    assert world.adapter.calls == []


def test_the_input_limit_narrows_the_total_window(world):
    world.adapter.models = {"model-a": {"context_length": 64000, "max_input_tokens": 48000}}
    caller, _ = world.destination()
    result = resolved(world, caller)
    assert result["window_tokens"] == 48000
    assert result["total_window_tokens"] == 64000
    assert result["max_input_tokens"] == 48000


def test_a_different_endpoint_does_not_reuse_the_previous_answer(world):
    world.adapter.models = {"model-a": {"context_length": 64000}}
    first, _ = world.destination()
    assert resolved(world, first)["window_tokens"] == 64000
    world.adapter.models = {"model-a": {"context_length": 32000}}
    second, _ = world.destination(endpoint="https://other.example/v1")
    assert resolved(world, second)["window_tokens"] == 32000


def test_a_failed_discovery_is_not_confirmed_nor_cached_as_data(world, clock):
    world.adapter.fail = True
    caller, _ = world.destination()
    result = resolved(world, caller)
    assert result["window_source"] == "fallback"
    assert result["window_estimated"] is True
    # Once the endpoint answers, the failure does not linger for an hour.
    world.adapter.fail = False
    world.adapter.models = {"model-a": {"context_length": 64000}}
    clock[0] += 61
    assert resolved(world, caller)["window_tokens"] == 64000


def test_offline_uses_the_last_discovery_saved_for_that_destination(world):
    world.adapter.models = {"model-a": {"context_length": 90000}}
    caller, _ = world.destination()
    world.models.refresh()
    world.adapter.fail = True
    windows.forget(world.ctx)
    result = resolved(world, caller)
    assert result["window_tokens"] == 90000
    assert result["window_source"] == "provider"


def test_an_explicit_refresh_does_not_wait_for_the_cache(world):
    world.adapter.models = {"model-a": {"context_length": 64000}}
    caller, _ = world.destination()
    assert resolved(world, caller)["window_tokens"] == 64000
    world.adapter.models = {"model-a": {"context_length": 32000}}
    world.models.refresh()
    assert resolved(world, caller)["window_tokens"] == 32000


def test_models_on_the_same_endpoint_share_one_discovery(world):
    world.adapter.models = {
        "model-a": {"context_length": 64000},
        "model-b": {"context_length": 32000},
    }
    first, _ = world.destination("model-a")
    second, _ = world.destination("model-b")
    assert resolved(world, first)["window_tokens"] == 64000
    assert resolved(world, second)["window_tokens"] == 32000
    assert len(world.adapter.calls) == 1


def test_refresh_saves_the_models_a_provider_started_offering_only_when_asked(world):
    world.adapter.models = {"model-a": {"context_length": 64000}}
    world.destination("model-a")
    world.adapter.models = {
        "model-a": {"context_length": 64000},
        "model-b": {"context_length": 32000},
    }
    assert all(result.added == () for result in world.models.refresh().values())
    assert [m.provider_model_id for m in world.models.list()] == ["model-a"]

    (result,) = world.models.refresh(add_new=True).values()
    assert result.added == ("model-b",)
    saved = {m.provider_model_id: m for m in world.models.list()}
    assert saved["model-b"].alias == "model-b"
    assert saved["model-b"].availability == "available"
    assert saved["model-b"].settings["discovered_capabilities"] == {"context_length": 32000}
    # Nothing new the second time.
    (again,) = world.models.refresh(add_new=True).values()
    assert again.added == ()


def test_an_added_model_never_takes_an_alias_already_in_use(world):
    world.adapter.models = {"model-a": {}, "model-b": {}}
    _, record = world.destination("model-a")
    world.models.alias(record.id, "model-b")
    (result,) = world.models.refresh(add_new=True).values()
    assert result.added == ("model-b-2",)


def test_adding_new_models_also_reads_providers_without_saved_models(world):
    world.adapter.models = {"model-z": {}}
    world.providers.add(
        AddProviderInput(
            alias="empty", provider_type="custom", endpoint="https://empty.example/v1", secret="sk"
        )
    )
    assert world.models.refresh()["empty"].added == ()
    assert world.models.refresh(add_new=True)["empty"].added == ("model-z",)
