"""Slice 3: provider/model management over the Engine Protocol.

Covers provider CRUD, health checks, discovery, selection, model
lifecycle/aliasing/use/discover/refresh/test, secret redaction, and the
keyring credential backend. Network is hermetic via httpx.MockTransport;
secrets never touch the real OS store (RINARI_KEYRING=0 in conftest plus
injected fakes).
"""

import json

import httpx
import pytest

from rinari.application.credentials import CredentialStore, parse_secret_ref
from rinari.application.services import build_services
from rinari.engine_protocol.server import EngineServer
from rinari.shared.errors import AuthenticationRequiredError, InvalidUsageError


@pytest.fixture
def services(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    return build_services(app_ctx, user_home=user_home)


@pytest.fixture
def server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


def _mock_models_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "mx-1"}, {"id": "mx-2"}]})
        return httpx.Response(404, json={"error": {"message": "nope"}})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def live_services(app_ctx, tmp_path):
    user_home = tmp_path / "live-home"
    user_home.mkdir()
    return build_services(app_ctx, http_client=_mock_models_client(), user_home=user_home)


@pytest.fixture
def live_server(live_services, tmp_path):
    engine = EngineServer(live_services, user_home=tmp_path / "live-home")
    yield engine
    engine.close()


def _call(server: EngineServer, method: str, params: dict | None = None, tag: str = "p"):
    line: dict = {"id": f"{tag}-{method}", "method": method}
    if params is not None:
        line["params"] = params
    return server.handle_line(json.dumps(line))


def _ok(response) -> dict:
    assert response is not None and response["ok"] is True, response
    return response["result"]


def _err(response) -> dict:
    assert response is not None and response["ok"] is False, response
    return response["error"]


def _create_local(server, alias="ollama", tag="p"):
    return _ok(
        _call(
            server,
            "provider.create",
            {
                "alias": alias,
                "type": "custom",
                "auth_method": "none",
                "endpoint": "http://127.0.0.1:11434/v1",
            },
            tag=tag,
        )
    )["provider"]


# -- provider CRUD ------------------------------------------------------------


def test_provider_create_no_auth_redacted_and_active(server) -> None:
    provider = _create_local(server)
    assert provider["alias"] == "ollama"
    assert provider["endpoint"] == "http://127.0.0.1:11434/v1"
    assert provider["has_credential"] is False
    assert provider["active"] is True
    assert "credential_ref" not in provider or provider["credential_ref"] is None


def test_provider_create_with_secret_never_echoed(server, services) -> None:
    result = _ok(
        _call(
            server,
            "provider.create",
            {
                "alias": "oai",
                "type": "openai",
                "auth_method": "api-key",
                "secret": "sk-test-value-123",
            },
        )
    )
    provider = result["provider"]
    assert provider["has_credential"] is True
    assert "sk-test-value-123" not in json.dumps(result)
    record = services.providers.get("oai")
    assert services.providers.resolve_secret(record) == "sk-test-value-123"


def test_provider_create_duplicate_alias_conflicts(server) -> None:
    _create_local(server)
    error = _err(_call(server, "provider.create", {"alias": "ollama", "type": "custom"}, tag="dup"))
    assert error["code"] == "CONFLICT"


def test_provider_create_unknown_type_rejected(server) -> None:
    error = _err(_call(server, "provider.create", {"alias": "x", "type": "nope"}, tag="bad"))
    assert error["code"] == "INVALID_USAGE"


def test_provider_get_not_found(server) -> None:
    error = _err(_call(server, "provider.get", {"ref": "ghost"}))
    assert error["code"] == "NOT_FOUND"


def test_provider_update_endpoint_invalidates_health(server) -> None:
    _create_local(server)
    provider = _ok(
        _call(server, "provider.update", {"ref": "ollama", "endpoint": "http://x:9/v1"})
    )["provider"]
    assert provider["endpoint"] == "http://x:9/v1"


def test_provider_update_nothing_rejected(server) -> None:
    _create_local(server)
    error = _err(_call(server, "provider.update", {"ref": "ollama"}))
    assert error["code"] == "INVALID_PARAMS"


def test_provider_remove_active_requires_switch(server) -> None:
    _create_local(server, alias="a")
    _create_local(server, alias="b", tag="b")
    error = _err(_call(server, "provider.remove", {"ref": "a"}, tag="rm"))
    assert error["code"] == "INVALID_USAGE"
    removed = _ok(_call(server, "provider.remove", {"ref": "a", "switch_to": "b"}, tag="rm2"))[
        "removed"
    ]
    assert removed["alias"] == "a"
    active = _ok(_call(server, "provider.list", tag="ls"))
    assert active["active_alias"] == "b"


def test_provider_use_switches_active(server) -> None:
    _create_local(server, alias="a")
    _create_local(server, alias="b", tag="b")
    selection = _ok(_call(server, "provider.use", {"ref": "b"}, tag="use"))
    assert selection["provider"]["alias"] == "b"
    assert selection["provider"]["active"] is True


# -- health / discovery (mocked network) --------------------------------------


def test_provider_test_reports_catalog(live_server) -> None:
    _create_local(live_server)
    health = _ok(_call(live_server, "provider.test", {"ref": "ollama"}, tag="t"))
    assert health["connected"] is True
    assert health["models_discovered"] == 2
    assert [m["provider_model_id"] for m in health["models"]] == ["mx-1", "mx-2"]
    provider = _ok(_call(live_server, "provider.get", {"ref": "ollama"}, tag="g"))["provider"]
    assert provider["status_connected"] is True


def test_model_discover_lists_without_saving(live_server) -> None:
    _create_local(live_server)
    found = _ok(_call(live_server, "model.discover", {"provider": "ollama"}, tag="d"))
    assert [m["provider_model_id"] for m in found["providers"]["ollama"]] == ["mx-1", "mx-2"]
    models = _ok(_call(live_server, "model.list", {"provider": "ollama"}, tag="l"))["models"]
    assert models == []


def test_provider_discover_env_candidate(server, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-env-key")
    candidates = _ok(_call(server, "provider.discover", tag="disc"))["candidates"]
    env_hits = [c for c in candidates if c["source"] == "environment"]
    assert any(c["provider_type"] == "openai" for c in env_hits)


# -- model lifecycle ----------------------------------------------------------


def test_model_add_alias_use_remove_roundtrip(server) -> None:
    _create_local(server)
    model = _ok(
        _call(
            server,
            "model.add",
            {"provider": "ollama", "provider_model_id": "mx-1", "alias": "main-coding"},
            tag="add",
        )
    )["model"]
    assert model["alias"] == "main-coding"
    assert model["active"] is True

    renamed = _ok(
        _call(
            server,
            "model.alias",
            {"ref": "main-coding", "new_alias": "principal"},
            tag="al",
        )
    )["model"]
    assert renamed["alias"] == "principal"

    used = _ok(_call(server, "model.use", {"ref": "principal"}, tag="use"))
    assert used["model"]["active"] is True
    assert used["provider"]["alias"] == "ollama"

    removed = _ok(_call(server, "model.remove", {"ref": "principal"}, tag="rm"))["removed"]
    assert removed["alias"] == "principal"
    error = _err(_call(server, "model.get", {"ref": "principal"}, tag="get"))
    assert error["code"] == "NOT_FOUND"


def test_model_test_and_refresh_with_mock(live_server) -> None:
    _create_local(live_server)
    _ok(
        _call(
            live_server,
            "model.add",
            {"provider": "ollama", "provider_model_id": "mx-1", "alias": "one"},
            tag="add",
        )
    )
    _ok(
        _call(
            live_server,
            "model.add",
            {"provider": "ollama", "provider_model_id": "stale", "alias": "old"},
            tag="add2",
        )
    )
    result = _ok(_call(live_server, "model.test", {"ref": "one"}, tag="t"))
    assert result["ok"] is True

    refreshed = _ok(_call(live_server, "model.refresh", {"provider": "ollama"}, tag="r"))[
        "providers"
    ]["ollama"]
    assert refreshed["still_available"] == 1
    assert refreshed["marked_unavailable"] == 1


# -- keyring backend ----------------------------------------------------------


class _FakeKeyring:
    def __init__(self) -> None:
        self.vault: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, key: str, secret: str) -> None:
        self.vault[(service, key)] = secret

    def get_password(self, service: str, key: str):
        return self.vault.get((service, key))

    def delete_password(self, service: str, key: str) -> None:
        try:
            del self.vault[(service, key)]
        except KeyError:
            # Real backends raise PasswordDeleteError; both map to False.
            raise KeyError(f"missing: {key}") from None


def test_keyring_scheme_roundtrip_with_fake_backend(app_ctx) -> None:
    store = CredentialStore(app_ctx.layout, keyring_backend=_FakeKeyring())
    ref = store.store_provider_secret("prov1", "s3cret")
    assert ref == "keyring://providers/prov1"
    assert parse_secret_ref(ref).scheme == "keyring"
    assert store.resolve(ref) == "s3cret"
    assert store.exists(ref) is True
    assert store.delete(ref) is True
    assert store.exists(ref) is False


def test_keyring_unavailable_resolve_raises(app_ctx) -> None:
    store = CredentialStore(app_ctx.layout, keyring_backend=None)
    with pytest.raises(AuthenticationRequiredError):
        store.resolve("keyring://providers/prov1")


def test_file_fallback_when_keyring_disabled(app_ctx) -> None:
    store = CredentialStore(app_ctx.layout, keyring_backend=None)
    ref = store.store_provider_secret("prov1", "s3cret")
    assert ref == "file://providers/prov1"
    assert store.resolve(ref) == "s3cret"


def test_keyring_ref_rejects_traversal() -> None:
    with pytest.raises(InvalidUsageError):
        parse_secret_ref("keyring://../escape")
