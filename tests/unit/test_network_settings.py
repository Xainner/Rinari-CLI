"""Red (plan 0.4.0 #96): network.json, the one HTTP client factory, network.* methods."""

from __future__ import annotations

import json
import os
import ssl
import time

import certifi
import httpcore
import httpx
import pytest

from rinari import net
from rinari.net import http as net_http
from rinari.net import probe

from . import test_engine_turns as base
from .test_engine_turns import _req

services = base.services
server = base.server

_ids = iter(range(1, 10_000))
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


@pytest.fixture(autouse=True)
def _no_ambient_proxy(monkeypatch):
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    # Windows reads the system proxy from the registry: none in these tests.
    monkeypatch.setattr("urllib.request.getproxies_registry", lambda: {}, raising=False)


def _call(server, method, params=None):
    return server.handle_line(_req(f"n{next(_ids)}", method, params))


def _write(home, value):
    home.mkdir(parents=True, exist_ok=True)
    path = home / "network.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    # A new mtime even on coarse clocks, so the cache sees the change.
    stamp = time.time() + next(_ids)
    os.utime(path, (stamp, stamp))


def _pool(client: httpx.Client, url: str):
    return client._transport_for_url(httpx.URL(url))


# -- settings --------------------------------------------------------------------------


def test_defaults_without_a_file(tmp_path) -> None:
    settings = net.load_settings(tmp_path)
    assert settings == net.NetworkSettings()
    assert settings.to_json() == {
        "proxy": {"mode": "system", "url": None, "bypass": []},
        "certificates": {"mode": "system", "path": None},
    }


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"proxy": {"mode": "pac"}}, "Unknown proxy mode"),
        ({"certificates": {"mode": "windows"}}, "Unknown certificates mode"),
        ({"proxy": {"mode": "manual"}}, "manual proxy needs its URL"),
        ({"proxy": {"mode": "manual", "url": "ftp://host:21"}}, "http://host:port"),
        ({"proxy": {"mode": "manual", "url": "http://p:1", "bypass": ["http://x"]}}, "bypass"),
        ({"certificates": {"mode": "file"}}, "need the path"),
        ({"certificates": {"mode": "file", "path": "C:/nope/ca.pem"}}, "does not exist"),
        ({"dns": {}}, "Unknown network setting"),
        ({"proxy": {"mode": "none", "port": 1}}, "Unknown network setting 'proxy.port'"),
        ([], "must be an object"),
    ],
)
def test_validation_says_what_is_wrong(value, message) -> None:
    with pytest.raises(ValueError, match=message):
        net.validate(value)


def test_a_file_that_is_not_pem_is_refused(tmp_path) -> None:
    bogus = tmp_path / "ca.pem"
    bogus.write_text("not a certificate", encoding="utf-8")
    with pytest.raises(ValueError, match="not a PEM bundle"):
        net.validate({"certificates": {"mode": "file", "path": str(bogus)}})


def test_save_is_atomic_and_round_trips(tmp_path) -> None:
    saved = net.save_settings(
        tmp_path,
        {
            "proxy": {"mode": "manual", "url": " http://proxy:8080 ", "bypass": ["*.corp", ""]},
            "certificates": {"mode": "file", "path": certifi.where()},
        },
    )
    assert saved.proxy_url == "http://proxy:8080"
    assert saved.proxy_bypass == ("*.corp",)
    assert net.load_settings(tmp_path) == saved
    assert [p.name for p in tmp_path.iterdir()] == ["network.json"]


def test_a_broken_file_falls_back_to_the_defaults_and_says_why(tmp_path) -> None:
    _write(tmp_path, {"proxy": {"mode": "pac"}})
    settings, error = net.settings_for(tmp_path)
    assert settings == net.NetworkSettings()
    assert "Unknown proxy mode" in error
    assert net.effective(tmp_path)["error"] == error
    # The factory still builds clients.
    net.client(tmp_path).close()


def test_settings_follow_the_file(tmp_path) -> None:
    assert net.settings_for(tmp_path)[0].proxy == "system"
    _write(tmp_path, {"proxy": {"mode": "none"}})
    assert net.settings_for(tmp_path)[0].proxy == "none"
    _write(tmp_path, {"proxy": {"mode": "manual", "url": "http://p:3128"}})
    assert net.settings_for(tmp_path)[0].proxy_url == "http://p:3128"


# -- the factory -----------------------------------------------------------------------


def test_system_proxy_comes_from_the_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy:3128")
    with net.client(tmp_path) as client:
        assert isinstance(_pool(client, "https://api.example.com")._pool, httpcore.HTTPProxy)
    assert net.effective(tmp_path)["proxy"]["source"] == "environment"


def test_none_ignores_the_environment_proxy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy:3128")
    _write(tmp_path, {"proxy": {"mode": "none"}})
    with net.client(tmp_path) as client:
        assert not isinstance(_pool(client, "https://api.example.com")._pool, httpcore.HTTPProxy)
    assert net.effective(tmp_path)["proxy"]["http"] is None


def test_manual_proxy_keeps_local_and_bypassed_hosts_direct(tmp_path) -> None:
    _write(
        tmp_path,
        {"proxy": {"mode": "manual", "url": "http://user:secret@proxy:8080", "bypass": ["*.corp"]}},
    )
    with net.client(tmp_path) as client:
        assert isinstance(_pool(client, "https://api.example.com")._pool, httpcore.HTTPProxy)
        for direct in ("http://localhost:11434", "http://127.0.0.1:1234", "http://[::1]:8000"):
            assert not isinstance(_pool(client, direct)._pool, httpcore.HTTPProxy), direct
        assert not isinstance(_pool(client, "https://llm.corp/v1")._pool, httpcore.HTTPProxy)
    effective = net.effective(tmp_path)["proxy"]
    assert effective["https"] == "http://***@proxy:8080"
    assert "secret" not in json.dumps(effective)


def test_system_certificates_use_truststore(tmp_path, monkeypatch) -> None:
    import truststore

    built = []

    class FakeContext(ssl.SSLContext):
        def __new__(cls, protocol):
            built.append(protocol)
            return super().__new__(cls, protocol)

    monkeypatch.setattr(truststore, "SSLContext", FakeContext)
    monkeypatch.setattr(net_http, "_ssl_cache", {})
    with net.client(tmp_path) as client:
        assert isinstance(client._transport._pool._ssl_context, FakeContext)
    assert built == [ssl.PROTOCOL_TLS_CLIENT]


def test_certifi_and_file_certificates(tmp_path) -> None:
    _write(tmp_path, {"certificates": {"mode": "certifi"}})
    with net.client(tmp_path) as client:
        context = client._transport._pool._ssl_context
        assert type(context) is ssl.SSLContext
        assert context.cert_store_stats()["x509_ca"] > 50
    _write(tmp_path, {"certificates": {"mode": "file", "path": certifi.where()}})
    with net.client(tmp_path) as client:
        context = client._transport._pool._ssl_context
        assert type(context) is ssl.SSLContext
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert net.effective(tmp_path)["certificates"]["source"].endswith(".pem")


def test_contexts_are_built_once_per_value(tmp_path) -> None:
    with net.client(tmp_path) as first, net.client(tmp_path) as second:
        assert first._transport._pool._ssl_context is second._transport._pool._ssl_context


def test_caller_transport_and_verify_win(tmp_path) -> None:
    _write(tmp_path, {"proxy": {"mode": "manual", "url": "http://proxy:8080"}})
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    with net.client(tmp_path, transport=transport, timeout=3.0, headers={"X-A": "1"}) as client:
        assert client.get("https://api.example.com/v1").json() == {"ok": True}
        assert client.headers["X-A"] == "1"
        assert client.timeout.connect == 3.0
    context = ssl.create_default_context()
    with net.client(tmp_path, verify=context) as client:
        assert client._transport._pool._ssl_context is context


def test_async_client_gets_the_same_settings(tmp_path) -> None:
    _write(tmp_path, {"proxy": {"mode": "manual", "url": "http://proxy:8080"}})
    client = net.async_client(tmp_path, timeout=5.0)
    assert isinstance(client, httpx.AsyncClient)
    assert isinstance(
        client._transport_for_url(httpx.URL("https://a.example"))._pool, httpcore.AsyncHTTPProxy
    )


def test_shared_client_follows_the_file(tmp_path) -> None:
    shared = net.shared_client(tmp_path, timeout=10.0)
    first = shared.current()
    assert shared.current() is first
    assert shared.timeout.connect == 10.0  # behaves as the httpx.Client it wraps
    _write(tmp_path, {"proxy": {"mode": "manual", "url": "http://proxy:8080"}})
    second = shared.current()
    assert second is not first
    assert isinstance(_pool(second, "https://a.example")._pool, httpcore.HTTPProxy)
    shared.close()
    assert first.is_closed and second.is_closed and shared.is_closed


# -- protocol ----------------------------------------------------------------------------


def test_network_settings_get_and_set(server) -> None:
    got = _call(server, "network.settings.get")
    assert got["ok"], got
    assert got["result"]["proxy"]["mode"] == "system"
    assert got["result"]["certificates"]["mode"] == "system"
    assert got["result"]["effective"]["certificates"]["source"] == "os"

    saved = _call(
        server, "network.settings.set", {"proxy": {"mode": "manual", "url": "http://p:3128"}}
    )
    assert saved["ok"], saved
    assert saved["result"]["proxy"] == {"mode": "manual", "url": "http://p:3128", "bypass": []}
    assert saved["result"]["certificates"]["mode"] == "system"  # kept
    assert saved["result"]["effective"]["proxy"]["source"] == "settings"
    home = server._services.ctx.home
    stored = json.loads((home / "network.json").read_text(encoding="utf-8"))
    assert stored["proxy"]["url"] == "http://p:3128"

    certs = _call(server, "network.settings.set", {"certificates": {"mode": "certifi"}})
    assert certs["ok"], certs
    assert certs["result"]["proxy"]["mode"] == "manual"  # kept
    assert _call(server, "network.settings.get")["result"]["certificates"]["mode"] == "certifi"


def test_network_settings_set_rejects_what_is_wrong(server) -> None:
    for params in (
        {"proxy": {"mode": "manual"}},
        {"certificates": {"mode": "file", "path": "C:/nope.pem"}},
        {"dns": "8.8.8.8"},
        {},
    ):
        response = _call(server, "network.settings.set", params)
        assert response["ok"] is False, params
        assert response["error"]["code"] == "INVALID_PARAMS"
    assert _call(server, "network.settings.get", {"x": 1})["error"]["code"] == "INVALID_PARAMS"
    assert not (server._services.ctx.home / "network.json").exists()


def _mock_factory(monkeypatch, handler, seen=None):
    def factory(home, **kwargs):
        if seen is not None:
            seen.append((home, kwargs))
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(probe, "net_client", factory)


def test_network_test_reaches_each_enabled_provider(server, monkeypatch) -> None:
    from rinari.application.provider_service import AddProviderInput

    providers = server._services.providers
    providers.add(
        AddProviderInput(
            alias="off",
            provider_type="custom",
            endpoint="http://10.0.0.9:8000/v1",
            auth_method="none",
        )
    )
    providers.set_enabled("off", False)
    seen = []
    requested = []

    def handler(request):
        requested.append(request)
        return httpx.Response(401, json={"error": "no key"})

    _mock_factory(monkeypatch, handler, seen)
    response = _call(server, "network.test")
    assert response["ok"], response
    results = response["result"]["results"]
    assert [row["alias"] for row in results] == ["fake"]
    row = results[0]
    assert row["ok"] is True and row["status"] == 401 and row["error"] is None
    assert row["url"] == "http://127.0.0.1:9/v1" and isinstance(row["latency_ms"], int)
    assert seen[0][0] == server._services.ctx.home
    assert "authorization" not in {key.lower() for key in requested[0].headers}
    assert response["result"]["effective"]["proxy"]["mode"] == "system"


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed"), "tls"),
        (httpx.ProxyError("407 Proxy Authentication Required"), "proxy"),
        (httpx.ConnectTimeout("timed out"), "timeout"),
        (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), "connect"),
    ],
)
def test_network_test_reports_why_it_failed(server, monkeypatch, error, kind) -> None:
    def handler(request):
        raise error

    _mock_factory(monkeypatch, handler)
    row = _call(server, "network.test")["result"]["results"][0]
    assert row["ok"] is False and row["status"] is None
    assert row["error_kind"] == kind and row["error"]


def test_network_test_has_an_overall_timeout(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    def handler(request):
        time.sleep(1.0)
        return httpx.Response(200)

    _mock_factory(monkeypatch, handler)
    record = SimpleNamespace(
        id="prv_1",
        alias="slow",
        type="custom",
        auth_method="none",
        endpoint="http://127.0.0.1:9/v1",
        settings={},
        enabled=True,
    )
    started = time.monotonic()
    rows = probe.probe_providers([record], tmp_path, overall_timeout=0.2)
    assert time.monotonic() - started < 0.9
    assert rows[0]["ok"] is False and rows[0]["error_kind"] == "timeout"


def test_network_test_validates_params(server) -> None:
    assert _call(server, "network.test", {"provider_ids": "fake"})["error"]["code"] == (
        "INVALID_PARAMS"
    )
    assert _call(server, "network.test", {"x": 1})["error"]["code"] == "INVALID_PARAMS"
