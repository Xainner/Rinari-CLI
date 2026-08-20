import httpx
import pytest

from rinari.providers.adapters.anthropic import AnthropicAdapter
from rinari.providers.adapters.base import AuthStatus, DiscoveredModel, ProviderHealth
from rinari.providers.adapters.openai_compatible import OpenAICompatibleAdapter
from rinari.providers.registry import (
    PROVIDER_TYPES,
    adapter_for,
    validate_provider_type,
)
from rinari.shared.errors import (
    ConfigurationError,
    InvalidUsageError,
    NetworkError,
    ProviderModelError,
)
from rinari.storage.records import ProviderRecord

NOW = "2023-11-14T22:13:20.000Z"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _openai_payload() -> dict:
    return {"data": [{"id": "gpt-mini"}, {"id": "gpt-main"}]}


def _anthropic_payload() -> dict:
    return {"data": [{"id": "claude-opus"}, {"id": "claude-sonnet"}]}


def test_openai_list_models() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_openai_payload())

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    models = adapter.list_models("sk-test", None)
    assert [m.provider_model_id for m in models] == ["gpt-mini", "gpt-main"]
    assert seen[0].url == "https://api.test/v1/models"
    assert seen[0].headers["Authorization"] == "Bearer sk-test"


def test_openai_no_auth_local() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"data": [{"id": "qwen3-coder"}]})

    adapter = OpenAICompatibleAdapter("http://127.0.0.1:11434/v1", client=_client(handler))
    assert adapter.validate_credential(None, None) == AuthStatus(connected=True, detail="ok")
    assert [m.provider_model_id for m in adapter.list_models(None, None)] == ["qwen3-coder"]


def test_openai_auth_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    status = adapter.validate_credential("sk-bad", None)
    assert status.connected is False
    assert "401" in status.detail

    with pytest.raises(ProviderModelError) as excinfo:
        adapter.list_models("sk-bad", None)
    assert "Authentication failed" in str(excinfo.value.message)


def test_openai_endpoint_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    status = adapter.validate_credential("sk-x", None)
    assert status.connected is False
    assert "503" in status.detail


def test_openai_missing_endpoint_rejected() -> None:
    adapter = OpenAICompatibleAdapter(None)
    with pytest.raises(ConfigurationError):
        adapter.validate_credential(None, None)


class _BytesStream(httpx.SyncByteStream):
    """A minimal sync stream whose body is not preloaded (is_stream_consumed False)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._sent = False

    def __iter__(self):
        if not self._sent:
            self._sent = True
            yield self._data

    def close(self) -> None:
        return None


def test_provider_error_detail_reads_stream_body() -> None:
    # Regression: a streaming error response is unread when passed to
    # provider_error_detail; it must read the body instead of crashing with
    # httpx.ResponseNotRead (which used to mask the real provider error).
    from rinari.providers.adapters.http import provider_error_detail

    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    resp = httpx.Response(
        503,
        request=req,
        stream=_BytesStream(b'{"error": {"message": "unknown model"}}'),
    )
    assert resp.is_stream_consumed is False
    detail = provider_error_detail(resp, "https://api.test/v1")
    assert "503" in detail
    assert "unknown model" in detail


def test_openai_timeout_maps_to_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    adapter = OpenAICompatibleAdapter("http://127.0.0.1:9/v1", client=_client(handler))
    with pytest.raises(NetworkError):
        adapter.validate_credential(None, None)


def test_anthropic_validate_and_models() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_anthropic_payload())

    adapter = AnthropicAdapter(client=_client(handler))
    assert adapter.validate_credential("sk-ant", None).connected is True
    assert seen[0].headers["x-api-key"] == "sk-ant"
    assert seen[0].headers["anthropic-version"] == "2023-06-01"

    models = adapter.list_models("sk-ant", None)
    assert [m.provider_model_id for m in models] == ["claude-opus", "claude-sonnet"]


def test_anthropic_auth_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    adapter = AnthropicAdapter(client=_client(handler))
    status = adapter.validate_credential("sk-bad", None)
    assert status.connected is False


def test_health_aggregates_discovery() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openai_payload())

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    health = adapter.health("sk-test", None)
    assert health == ProviderHealth(
        connected=True,
        detail="2 model(s) discovered",
        models_discovered=2,
        models=[DiscoveredModel("gpt-mini"), DiscoveredModel("gpt-main")],
    )


def test_health_auth_failure_short_circuits_discovery() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    health = adapter.health("sk-bad", None)
    assert health.connected is False
    assert health.models_discovered == 0


def test_registry_dispatch_by_type() -> None:
    record = ProviderRecord(
        id="prov_1",
        alias="o",
        type="openai",
        auth_method="api-key",
        account_hint=None,
        endpoint=None,
        settings={},
        default_model_id=None,
        last_used_model_id=None,
        status_connected=None,
        status_checked_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    assert isinstance(adapter_for(record), OpenAICompatibleAdapter)

    anthropic = ProviderRecord(
        id="prov_2",
        alias="a",
        type="anthropic",
        auth_method="api-key",
        account_hint=None,
        endpoint=None,
        settings={},
        default_model_id=None,
        last_used_model_id=None,
        status_connected=None,
        status_checked_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    assert isinstance(adapter_for(anthropic), AnthropicAdapter)


def test_registry_custom_with_anthropic_protocol() -> None:
    record = ProviderRecord(
        id="prov_3",
        alias="c",
        type="custom",
        auth_method="api-key",
        account_hint=None,
        endpoint="http://localhost:9090",
        settings={"protocol": "anthropic-compatible"},
        default_model_id=None,
        last_used_model_id=None,
        status_connected=None,
        status_checked_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    assert isinstance(adapter_for(record), AnthropicAdapter)


def test_registry_unknown_protocol() -> None:
    record = ProviderRecord(
        id="prov_4",
        alias="c",
        type="custom",
        auth_method="api-key",
        account_hint=None,
        endpoint="http://localhost:9090",
        settings={"protocol": "gemini-native"},
        default_model_id=None,
        last_used_model_id=None,
        status_connected=None,
        status_checked_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(InvalidUsageError):
        adapter_for(record)


def test_validate_provider_type() -> None:
    assert validate_provider_type("openai", "api-key").adapter is OpenAICompatibleAdapter
    assert validate_provider_type("custom", "none", protocol="anthropic-compatible")
    with pytest.raises(InvalidUsageError):
        validate_provider_type("gemini", "api-key")
    with pytest.raises(InvalidUsageError):
        validate_provider_type("anthropic", "none")
    with pytest.raises(InvalidUsageError):
        validate_provider_type("custom", "api-key", protocol="bogus")
    assert set(PROVIDER_TYPES) == {"openai", "anthropic", "custom"}
