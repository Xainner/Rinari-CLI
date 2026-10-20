"""OAuth for remote MCP servers (plan de conectores C1).

One in-process HTTP server on 127.0.0.1 plays both roles: a Streamable HTTP
MCP server (the protected resource) and its OAuth authorization server
(metadata, dynamic registration, authorize with PKCE, token, revocation).
The "browser" is httpx following the authorize redirect to Rinari's loopback
callback. No external network, no real service, a temporary RINARI_HOME.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from rinari.application.services import build_services
from rinari.engine_protocol.protocol import CAPABILITIES
from rinari.engine_protocol.server import EngineServer
from rinari.mcp import oauth as oauth_module
from rinari.mcp.client import McpClient, McpError
from rinari.mcp.config import McpConfigError
from rinari.mcp.http_transport import StreamableHttpTransport
from rinari.mcp.oauth import (
    OAuthError,
    OAuthManager,
    OAuthSession,
    OAuthStore,
    authorization_server_metadata_urls,
    canonical_resource,
    discover,
    parse_challenge,
    resource_metadata_urls,
)

OWN_CLIENT_ID = "own-client-id"
OWN_CLIENT_SECRET = "own-client-secret-xyz"


class FakeAuth:
    """Scripted MCP resource server plus its authorization server."""

    def __init__(self) -> None:
        self.base = ""
        # Discovery.
        self.publish_prm = True  # /.well-known/oauth-protected-resource/mcp
        self.prm_path = "/.well-known/oauth-protected-resource/mcp"
        self.challenge_names_prm = True
        # Authorization server features.
        self.registration = True
        self.cimd = False
        self.revocation = True
        self.scopes_supported = ["mcp.read", "mcp.write"]
        # Resource behaviour.
        self.anonymous_listing = False  # Google: initialize/tools.list need no token
        # Tokens.
        self.expires_in = 3600
        self.reject_refresh = False
        self.rotate_refresh = True
        self.clients: dict[str, dict[str, Any]] = {}
        self.registrations: list[dict[str, Any]] = []
        self.codes: dict[str, dict[str, Any]] = {}
        self.access: dict[str, str] = {}  # access token -> client_id
        self.refresh: dict[str, str] = {}  # refresh token -> client_id
        self.revoked: list[str] = []
        self.authorize_requests: list[dict[str, str]] = []
        self.token_requests: list[dict[str, Any]] = []
        self.mcp_auth_headers: list[str | None] = []
        self.counter = itertools.count(1)
        self.lock = threading.Lock()

    # -- helpers -------------------------------------------------------------------

    @property
    def mcp_url(self) -> str:
        return f"{self.base}/mcp"

    def issue(self, client_id: str) -> dict[str, Any]:
        n = next(self.counter)
        access, refresh = f"at-{n}-secret", f"rt-{n}-secret"
        self.access[access] = client_id
        self.refresh[refresh] = client_id
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "scope": " ".join(self.scopes_supported),
        }

    def invalidate_access(self) -> None:
        self.access.clear()

    def client_ok(self, client_id: str, redirect_uri: str) -> bool:
        if client_id in self.clients:
            return redirect_uri in self.clients[client_id]["redirect_uris"]
        if client_id == OWN_CLIENT_ID:
            return redirect_uri.startswith("http://127.0.0.1:")
        if self.cimd and client_id.startswith("https://"):
            return redirect_uri.startswith("http://127.0.0.1:")
        return False

    def authenticate(self, headers, form: dict[str, str]) -> str | None:
        """client_id of the token/revocation request, or None when it fails."""
        basic = headers.get("Authorization", "")
        if basic.startswith("Basic "):
            client_id, _, secret = base64.b64decode(basic[6:]).decode().partition(":")
        else:
            client_id, secret = form.get("client_id", ""), form.get("client_secret", "")
        if client_id == OWN_CLIENT_ID:
            return client_id if secret == OWN_CLIENT_SECRET else None
        if client_id in self.clients or (self.cimd and client_id.startswith("https://")):
            return client_id
        return None

    def as_metadata(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "issuer": self.base,
            "authorization_endpoint": f"{self.base}/authorize",
            "token_endpoint": f"{self.base}/token",
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
        }
        if self.registration:
            meta["registration_endpoint"] = f"{self.base}/register"
        if self.revocation:
            meta["revocation_endpoint"] = f"{self.base}/revoke"
        if self.cimd:
            meta["client_id_metadata_document_supported"] = True
        return meta


def _handler(state: FakeAuth):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return

        def _json(self, status: int, body: Any, headers: dict | None = None) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length") or 0)
            return {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            if parts.path == state.prm_path and state.publish_prm:
                self._json(
                    200,
                    {
                        "resource": state.mcp_url,
                        "authorization_servers": [state.base],
                        "scopes_supported": state.scopes_supported,
                    },
                )
                return
            if parts.path == "/.well-known/oauth-authorization-server":
                self._json(200, state.as_metadata())
                return
            if parts.path == "/authorize":
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                state.authorize_requests.append(query)
                redirect = query.get("redirect_uri", "")
                if (
                    query.get("response_type") != "code"
                    or query.get("code_challenge_method") != "S256"
                    or not query.get("code_challenge")
                    or not query.get("state")
                    or query.get("resource") != state.mcp_url
                    or not state.client_ok(query.get("client_id", ""), redirect)
                ):
                    self._json(400, {"error": "invalid_request"})
                    return
                code = f"code-{next(state.counter)}"
                state.codes[code] = query
                target = f"{redirect}?{urlencode({'code': code, 'state': query['state']})}"
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            parts = urlsplit(self.path)
            if parts.path == "/register":
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length))
                state.registrations.append(body)
                if not state.registration:
                    self._json(404, {"error": "not_found"})
                    return
                client_id = f"dcr-{next(state.counter)}"
                state.clients[client_id] = {"redirect_uris": body["redirect_uris"]}
                self._json(
                    201,
                    {
                        "client_id": client_id,
                        "redirect_uris": body["redirect_uris"],
                        "token_endpoint_auth_method": "none",
                    },
                )
                return
            if parts.path == "/token":
                form = self._form()
                with state.lock:
                    state.token_requests.append(
                        {"form": form, "auth": self.headers.get("Authorization")}
                    )
                client_id = state.authenticate(self.headers, form)
                if client_id is None:
                    self._json(401, {"error": "invalid_client"})
                    return
                if form.get("grant_type") == "authorization_code":
                    request = state.codes.pop(form.get("code", ""), None)
                    verifier = form.get("code_verifier", "")
                    challenge = (
                        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                        .rstrip(b"=")
                        .decode()
                    )
                    if (
                        request is None
                        or request["client_id"] != client_id
                        or request["code_challenge"] != challenge
                        or request["redirect_uri"] != form.get("redirect_uri")
                        or form.get("resource") != state.mcp_url
                    ):
                        self._json(400, {"error": "invalid_grant"})
                        return
                    self._json(200, state.issue(client_id))
                    return
                if form.get("grant_type") == "refresh_token":
                    token = form.get("refresh_token", "")
                    if state.reject_refresh or state.refresh.get(token) != client_id:
                        self._json(
                            400,
                            {"error": "invalid_grant", "error_description": f"bad {token}"},
                        )
                        return
                    issued = state.issue(client_id)
                    if state.rotate_refresh:
                        state.refresh.pop(token, None)
                    else:
                        state.refresh.pop(issued["refresh_token"])
                        issued.pop("refresh_token")
                    self._json(200, issued)
                    return
                self._json(400, {"error": "unsupported_grant_type"})
                return
            if parts.path == "/revoke":
                form = self._form()
                if state.authenticate(self.headers, form) is None:
                    self._json(401, {"error": "invalid_client"})
                    return
                state.revoked.append(form.get("token", ""))
                state.access.pop(form.get("token", ""), None)
                state.refresh.pop(form.get("token", ""), None)
                self._json(200, {})
                return
            if parts.path == "/mcp":
                self._mcp()
                return
            self._json(404, {"error": "not_found"})

        def do_DELETE(self) -> None:
            self._json(200, {})

        def _mcp(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            msg = json.loads(self.rfile.read(length) or b"{}")
            auth = self.headers.get("Authorization")
            state.mcp_auth_headers.append(auth)
            method = msg.get("method")
            anonymous_ok = state.anonymous_listing and method in (
                "initialize",
                "tools/list",
                "notifications/initialized",
            )
            token = auth[7:] if auth and auth.startswith("Bearer ") else None
            if not anonymous_ok and token not in state.access:
                challenge = 'Bearer error="invalid_token"'
                if state.challenge_names_prm:
                    challenge += (
                        f', resource_metadata="{state.base}{state.prm_path}"'
                        f', scope="{" ".join(state.scopes_supported)}"'
                    )
                self._json(401, {"error": "invalid_token"}, {"WWW-Authenticate": challenge})
                return
            rid = msg.get("id")
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "oauth-fake", "version": "1"},
                    "capabilities": {"tools": {}},
                }
            elif method and method.startswith("notifications/"):
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            elif method == "tools/list":
                result = {"tools": [{"name": "whoami", "inputSchema": {"type": "object"}}]}
            elif method == "tools/call":
                result = {"content": [{"type": "text", "text": "you are signed in"}]}
            else:
                self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601}})
                return
            headers = {"Mcp-Session-Id": "s-1"} if method == "initialize" else {}
            self._json(200, {"jsonrpc": "2.0", "id": rid, "result": result}, headers)

    return Handler


@pytest.fixture
def fake():
    state = FakeAuth()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
def services(app_ctx, tmp_path):
    return build_services(app_ctx, user_home=tmp_path / "home")


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []
        self.lock = threading.Lock()

    def __call__(self, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.items.append((name, payload))

    def finished(self) -> list[dict[str, Any]]:
        return [p for n, p in self.items if n == "mcp.auth.finished"]

    def stages(self) -> list[str]:
        return [p["stage"] for n, p in self.items if n == "mcp.auth.progress"]


@pytest.fixture
def events():
    return Events()


@pytest.fixture
def opened():
    return []


@pytest.fixture
def manager(services, events, opened):
    instance = OAuthManager(
        services.credentials,
        home=services.ctx.home,
        opener=lambda url: opened.append(url) or True,
        on_event=events,
    )
    yield instance
    instance.close()


def _browser(url: str) -> httpx.Response:
    """The owner approving in the browser: follow the redirect to the callback."""
    with httpx.Client(trust_env=False, follow_redirects=True, timeout=10) as browser:
        return browser.get(url)


def _add(services, fake: FakeAuth, **auth: Any) -> dict:
    return services.mcp.add("remote", url=fake.mcp_url, auth={"kind": "oauth", **auth})


def _start(manager: OAuthManager, services, row: dict, **kwargs: Any) -> dict:
    from rinari.mcp.config import draft_from_row

    draft = draft_from_row(row)
    secret = None
    if draft.oauth and draft.oauth.client_secret is not None:
        secret = services.credentials.resolve(draft.oauth.client_secret.ref)
    return manager.start(
        row["id"], row["name"], row["url"], draft.oauth, client_secret=secret, **kwargs
    )


def _sign_in(manager, services, row) -> dict:
    flow = _start(manager, services, row)
    page = _browser(flow["authorize_url"])
    assert page.status_code == 200, page.text
    assert "Rinari is connected" in page.text
    return flow


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _home_text(app_ctx) -> str:
    """Everything Rinari wrote under its home except the credential store."""
    parts: list[str] = []
    credentials = app_ctx.layout.credentials_dir
    for path in app_ctx.layout.root.rglob("*"):
        if path.is_file() and credentials not in path.parents:
            parts.append(path.read_bytes().decode("latin-1"))
    return "\n".join(parts)


def _tokens(fake: FakeAuth) -> list[str]:
    return [*fake.access, *fake.refresh, *fake.revoked, OWN_CLIENT_SECRET]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_well_known_urls_follow_the_mcp_and_issuer_paths() -> None:
    assert resource_metadata_urls("https://mcp.example.com/v1/mcp/") == [
        "https://mcp.example.com/.well-known/oauth-protected-resource/v1/mcp",
        "https://mcp.example.com/.well-known/oauth-protected-resource",
    ]
    assert resource_metadata_urls("https://mcp.example.com") == [
        "https://mcp.example.com/.well-known/oauth-protected-resource"
    ]
    assert authorization_server_metadata_urls("https://auth.example.com/tenant") == [
        "https://auth.example.com/.well-known/oauth-authorization-server/tenant",
        "https://auth.example.com/.well-known/openid-configuration/tenant",
        "https://auth.example.com/tenant/.well-known/openid-configuration",
    ]
    assert canonical_resource("HTTPS://Mcp.Example.com/mcp/?x=1#f") == "https://mcp.example.com/mcp"
    assert parse_challenge('Bearer realm="x", resource_metadata="https://a/b", scope="r w"') == {
        "realm": "x",
        "resource_metadata": "https://a/b",
        "scope": "r w",
    }


def test_discovery_without_a_401_reads_the_resource_metadata(fake) -> None:
    fake.anonymous_listing = True  # like Google: initialize and tools/list answer anonymously
    with httpx.Client(trust_env=False) as http:
        metadata = discover(fake.mcp_url, http)
    assert metadata.resource_metadata is True
    assert metadata.resource == fake.mcp_url
    assert metadata.issuer == fake.base
    assert metadata.registration_endpoint == f"{fake.base}/register"
    assert metadata.scopes_supported == ("mcp.read", "mcp.write")
    # Found at the well-known URL: no anonymous initialize was needed.
    assert fake.mcp_auth_headers == []


def test_discovery_reads_the_challenge_when_metadata_is_elsewhere(fake) -> None:
    fake.prm_path = "/meta/prm.json"  # not at the well-known URL: only the 401 names it
    with httpx.Client(trust_env=False) as http:
        metadata = discover(fake.mcp_url, http)
    assert metadata.resource_metadata is True
    assert metadata.challenge_scopes == ("mcp.read", "mcp.write")
    assert fake.mcp_auth_headers == [None]  # one anonymous initialize


def test_discovery_falls_back_to_the_origin_without_resource_metadata(fake) -> None:
    fake.publish_prm = False  # like Atlassian: 404 and no resource_metadata in the 401
    fake.challenge_names_prm = False
    with httpx.Client(trust_env=False) as http:
        metadata = discover(fake.mcp_url, http)
    assert metadata.resource_metadata is False
    assert metadata.issuer == fake.base
    assert metadata.resource == fake.mcp_url
    assert metadata.token_endpoint == f"{fake.base}/token"


def test_discovery_without_any_metadata_is_a_stable_error() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
        with httpx.Client(trust_env=False) as http, pytest.raises(OAuthError) as excinfo:
            discover(url, http)
    finally:
        server.shutdown()
        server.server_close()
    assert (excinfo.value.code, excinfo.value.hint) == (
        "OAUTH_DISCOVERY_FAILED",
        "oauth_metadata_missing",
    )


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


def test_dynamic_registration_pkce_and_callback_sign_in(
    fake, services, manager, events, opened, app_ctx
) -> None:
    row = _add(services, fake)
    flow = _start(manager, services, row)
    assert flow["client"] == "registered"
    assert flow["browser_opened"] is True and opened == [flow["authorize_url"]]
    assert flow["redirect_uri"].startswith("http://127.0.0.1:")
    assert flow["redirect_uri"].endswith("/callback")
    registration = fake.registrations[0]
    assert registration["redirect_uris"] == [flow["redirect_uri"]]
    assert registration["token_endpoint_auth_method"] == "none"
    query = {k: v[0] for k, v in parse_qs(urlsplit(flow["authorize_url"]).query).items()}
    assert query["code_challenge_method"] == "S256"
    assert len(query["code_challenge"]) == 43
    assert query["resource"] == fake.mcp_url
    assert query["scope"] == "mcp.read mcp.write"
    assert len(query["state"]) >= 32
    assert "access_type" not in query  # only for issuers that need it

    page = _browser(flow["authorize_url"])
    assert page.status_code == 200 and "Rinari is connected" in page.text
    assert events.finished() == [
        {"name": "remote", "flow_id": flow["flow_id"], "outcome": "signed_in", "finished_at": ANY}
    ]
    assert events.stages() == [
        "discovering",
        "registering",
        "waiting_for_browser",
        "exchanging_code",
    ]
    status = manager.status(row["id"])
    assert status["signed_in"] is True and status["has_refresh_token"] is True
    assert status["scopes"] == ["mcp.read", "mcp.write"]
    assert status["client"] == "registered" and status["pending"] is None

    # The transport now carries the token: tools work.
    result = services.mcp.test("remote")
    assert result["ok"] is True, result
    assert fake.mcp_auth_headers[-1].startswith("Bearer at-")
    assert services.mcp.invoke("remote", "whoami", {})["content"][0]["text"] == "you are signed in"

    # Tokens live only in the credential store.
    stored = OAuthStore(services.credentials, row["id"]).load_tokens()
    assert stored is not None and stored["access_token"] in fake.access
    home = _home_text(app_ctx)
    for token in _tokens(fake):
        assert token not in home
        assert token not in json.dumps(status)
        assert token not in json.dumps(events.items)
        assert token not in json.dumps(services.mcp.view(services.mcp.show("remote")))


class _Any:
    def __eq__(self, other: object) -> bool:
        return True


ANY = _Any()


def test_registered_client_is_reused_on_its_port_and_replaced_when_busy(
    fake, services, manager
) -> None:
    row = _add(services, fake)
    first = _sign_in(manager, services, row)
    second = _sign_in(manager, services, row)
    assert len(fake.registrations) == 1
    assert second["redirect_uri"] == first["redirect_uri"]
    port = urlsplit(first["redirect_uri"]).port
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", port))
        busy.listen()
        third = _sign_in(manager, services, row)
    assert len(fake.registrations) == 2
    assert third["redirect_uri"] != first["redirect_uri"]


def test_client_metadata_document_when_the_server_does_not_register(
    fake, services, events, opened
) -> None:
    fake.registration = False
    fake.cimd = True
    row = _add(services, fake)
    without = OAuthManager(services.credentials, home=services.ctx.home, opener=lambda u: True)
    with pytest.raises(OAuthError) as excinfo:
        _start(without, services, row)
    assert (excinfo.value.code, excinfo.value.hint) == (
        "OAUTH_CLIENT_REQUIRED",
        "oauth_client_required",
    )
    document = "https://client.example.org/rinari/client.json"
    with_document = OAuthManager(
        services.credentials,
        home=services.ctx.home,
        opener=lambda u: True,
        on_event=events,
        client_metadata_url=document,
    )
    try:
        flow = _sign_in(with_document, services, row)
    finally:
        with_document.close()
    assert flow["client"] == "metadata_document"
    assert parse_qs(urlsplit(flow["authorize_url"]).query)["client_id"] == [document]
    assert fake.registrations == []
    assert events.finished()[-1]["outcome"] == "signed_in"


def test_configured_metadata_document_url_per_server(fake, services, manager) -> None:
    fake.registration = False
    fake.cimd = True
    document = "https://docs.example.net/oauth/rinari.json"
    row = _add(services, fake, client_metadata_url=document)
    flow = _sign_in(manager, services, row)
    assert flow["client"] == "metadata_document"


def test_own_client_id_and_secret(fake, services, manager, app_ctx) -> None:
    fake.registration = False
    row = _add(services, fake, client_id=OWN_CLIENT_ID, client_secret=OWN_CLIENT_SECRET)
    assert OWN_CLIENT_SECRET not in json.dumps(row)
    view = services.mcp.view(row)
    assert view["auth"]["client_id"] == OWN_CLIENT_ID
    assert view["auth"]["client_secret"] == {"configured": True, "source": "stored"}
    flow = _sign_in(manager, services, row)
    assert flow["client"] == "configured"
    exchange = fake.token_requests[-1]
    assert exchange["auth"].startswith("Basic ")  # client_secret_basic
    assert "client_secret" not in exchange["form"]
    # Refreshing uses the same client authentication.
    fake.invalidate_access()
    assert services.mcp.test("remote")["ok"] is True
    assert fake.token_requests[-1]["form"]["grant_type"] == "refresh_token"
    assert fake.token_requests[-1]["auth"].startswith("Basic ")
    assert OWN_CLIENT_SECRET not in _home_text(app_ctx)


def test_offline_access_is_asked_from_issuers_that_need_it(
    fake, services, manager, monkeypatch
) -> None:
    monkeypatch.setattr(oauth_module, "OFFLINE_ACCESS_ISSUERS", frozenset({fake.base}))
    fake.registration = False
    row = _add(services, fake, client_id=OWN_CLIENT_ID, client_secret=OWN_CLIENT_SECRET)
    first = _start(manager, services, row)
    query = parse_qs(urlsplit(first["authorize_url"]).query)
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    _browser(first["authorize_url"])
    # With a refresh token already kept, consent is not forced again.
    second = _start(manager, services, row)
    query = parse_qs(urlsplit(second["authorize_url"]).query)
    assert query["access_type"] == ["offline"] and "prompt" not in query
    manager.cancel(row["id"])


def test_wrong_state_is_rejected_and_the_sign_in_keeps_waiting(
    fake, services, manager, events
) -> None:
    row = _add(services, fake)
    flow = _start(manager, services, row)
    with httpx.Client(trust_env=False) as browser:
        forged = browser.get(f"{flow['redirect_uri']}?code=stolen&state=not-the-state")
        assert forged.status_code == 400
        assert browser.get(flow["redirect_uri"].replace("/callback", "/other")).status_code == 404
    assert events.finished() == []
    assert all(r["form"].get("code") != "stolen" for r in fake.token_requests)
    assert manager.status(row["id"])["pending"]["flow_id"] == flow["flow_id"]
    page = _browser(flow["authorize_url"])
    assert "Rinari is connected" in page.text
    assert events.finished()[-1]["outcome"] == "signed_in"


def test_fixed_redirect_port_in_use_is_a_stable_error(fake, services, manager) -> None:
    fake.registration = False
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        row = _add(services, fake, client_id=OWN_CLIENT_ID, redirect_port=port)
        with pytest.raises(OAuthError) as excinfo:
            _start(manager, services, row)
    assert (excinfo.value.code, excinfo.value.hint) == ("OAUTH_PORT_IN_USE", "redirect_port_busy")


def test_fixed_redirect_port_is_used_when_free(fake, services, manager) -> None:
    fake.registration = False
    port = _free_port()
    row = _add(
        services, fake, client_id=OWN_CLIENT_ID, client_secret=OWN_CLIENT_SECRET, redirect_port=port
    )
    flow = _sign_in(manager, services, row)
    assert flow["redirect_uri"] == f"http://127.0.0.1:{port}/callback"


def test_cancel_mid_flow_closes_the_listener(fake, services, manager, events) -> None:
    row = _add(services, fake)
    flow = _start(manager, services, row)
    assert manager.cancel(row["id"]) is True
    assert manager.cancel(row["id"]) is False
    finished = events.finished()[-1]
    assert finished["outcome"] == "cancelled"
    assert finished["error"]["hint"] == "oauth_cancelled"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            _browser(flow["authorize_url"])
        except httpx.ConnectError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the loopback listener is still open after cancel")
    assert manager.status(row["id"])["signed_in"] is False
    assert manager.status(row["id"])["last"]["outcome"] == "cancelled"


def test_denied_authorization_ends_the_sign_in(fake, services, manager, events) -> None:
    row = _add(services, fake)
    flow = _start(manager, services, row)
    state = parse_qs(urlsplit(flow["authorize_url"]).query)["state"][0]
    with httpx.Client(trust_env=False) as browser:
        page = browser.get(f"{flow['redirect_uri']}?error=access_denied&state={state}")
    assert page.status_code == 200
    finished = events.finished()[-1]
    assert finished["outcome"] == "failed"
    assert finished["error"]["code"] == "OAUTH_DENIED"


def test_sign_in_times_out(fake, services, manager, events) -> None:
    row = _add(services, fake)
    _start(manager, services, row, timeout_s=0.01)  # clamped to the 5 s minimum
    deadline = time.monotonic() + 10
    while not events.finished() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert events.finished()[-1]["outcome"] == "timed_out"
    assert events.finished()[-1]["error"]["hint"] == "oauth_timeout"


# ---------------------------------------------------------------------------
# Tokens on the wire
# ---------------------------------------------------------------------------


def test_token_is_refreshed_before_it_expires(fake, services, manager) -> None:
    row = _add(services, fake)
    _sign_in(manager, services, row)
    store = OAuthStore(services.credentials, row["id"])
    tokens = store.load_tokens()
    old_access = tokens["access_token"]
    clock = [time.time()]
    session = OAuthSession(store, clock=lambda: clock[0])
    assert session.authorization() == f"Bearer {old_access}"
    clock[0] = tokens["expires_at"] - 30  # inside the refresh window
    header = session.authorization()
    assert header != f"Bearer {old_access}"
    assert fake.token_requests[-1]["form"]["grant_type"] == "refresh_token"
    assert fake.token_requests[-1]["form"]["resource"] == fake.mcp_url
    stored = store.load_tokens()
    assert header == f"Bearer {stored['access_token']}"
    assert stored["refresh_token"] in fake.refresh  # rotated and kept


def test_a_kept_refresh_token_survives_an_answer_without_one(fake, services, manager) -> None:
    fake.rotate_refresh = False
    row = _add(services, fake)
    _sign_in(manager, services, row)
    store = OAuthStore(services.credentials, row["id"])
    before = store.load_tokens()["refresh_token"]
    fake.invalidate_access()
    assert services.mcp.test("remote")["ok"] is True
    assert store.load_tokens()["refresh_token"] == before


def test_401_refreshes_and_retries_once(fake, services, manager) -> None:
    row = _add(services, fake)
    _sign_in(manager, services, row)
    services.mcp.connect("remote")
    fake.invalidate_access()  # the server forgets the access token
    refreshes = sum(r["form"]["grant_type"] == "refresh_token" for r in fake.token_requests)
    result = services.mcp.invoke("remote", "whoami", {})
    assert result["content"][0]["text"] == "you are signed in"
    after = sum(r["form"]["grant_type"] == "refresh_token" for r in fake.token_requests)
    assert after == refreshes + 1
    assert fake.mcp_auth_headers[-2] != fake.mcp_auth_headers[-1]  # 401, then the retry


def test_failed_refresh_raises_reauth_required(fake, services, manager, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    row = _add(services, fake)
    _sign_in(manager, services, row)
    services.mcp.connect("remote")
    fake.invalidate_access()
    fake.reject_refresh = True
    with pytest.raises(McpError) as excinfo:
        services.mcp.invoke("remote", "whoami", {})
    assert excinfo.value.code == "MCP_AUTH_REJECTED"
    assert excinfo.value.hint == "reauth_required"
    assert excinfo.value.http_status == 401
    status = manager.status(row["id"])
    assert status["signed_in"] is True and status["has_refresh_token"] is False
    assert status["reauth_required"] is True
    # A refused refresh token is not spent again.
    requests = len(fake.token_requests)
    with pytest.raises(McpError) as again:
        services.mcp.invoke("remote", "whoami", {})
    assert again.value.hint == "reauth_required"
    assert len(fake.token_requests) == requests
    test = services.mcp.test("remote")
    assert (test["ok"], test["hint"]) == (False, "reauth_required")
    text = str(excinfo.value) + json.dumps(test) + json.dumps(services.mcp.logs()) + caplog.text
    for token in _tokens(fake):
        assert token not in text


def test_expired_token_without_refresh_needs_reauth(fake, services, manager) -> None:
    row = _add(services, fake)
    _sign_in(manager, services, row)
    store = OAuthStore(services.credentials, row["id"])
    tokens = store.load_tokens()
    store.save_tokens({**tokens, "refresh_token": None, "expires_at": time.time() - 10})
    with pytest.raises(McpError) as excinfo:
        services.mcp.connect("remote")
    assert excinfo.value.hint == "reauth_required"
    assert manager.status(row["id"])["reauth_required"] is True


def test_never_signed_in_server_reports_oauth_required(fake, services) -> None:
    _add(services, fake)
    result = services.mcp.test("remote")
    assert result["ok"] is False
    assert (result["code"], result["hint"]) == ("MCP_AUTH_REJECTED", "oauth_required")


def test_anonymous_listing_works_before_signing_in(fake, services) -> None:
    fake.anonymous_listing = True
    _add(services, fake)
    assert services.mcp.test("remote")["ok"] is True
    with pytest.raises(McpError) as excinfo:
        services.mcp.invoke("remote", "whoami", {})
    assert excinfo.value.hint == "oauth_required"


def test_a_connection_made_before_signing_in_uses_the_new_token(fake, services, manager) -> None:
    fake.anonymous_listing = True
    row = _add(services, fake)
    services.mcp.connect("remote")  # cached without a token
    _sign_in(manager, services, row)
    refreshes = sum(r["form"]["grant_type"] == "refresh_token" for r in fake.token_requests)
    result = services.mcp.invoke("remote", "whoami", {})
    assert result["content"][0]["text"] == "you are signed in"
    after = sum(r["form"]["grant_type"] == "refresh_token" for r in fake.token_requests)
    assert after == refreshes  # the stored token was enough: no refresh spent


def test_anonymous_discovery_ends_the_session_it_opened(fake) -> None:
    fake.anonymous_listing = True
    fake.publish_prm = False
    fake.challenge_names_prm = False
    deletes: list[str] = []
    original = fake.mcp_url

    with httpx.Client(trust_env=False) as http:
        http.event_hooks["request"] = [
            lambda request: (
                deletes.append(request.headers.get("Mcp-Session-Id"))
                if request.method == "DELETE"
                else None
            )
        ]
        assert oauth_module.anonymous_challenge(original, http) == {}
    assert deletes == ["s-1"]


def test_unsupported_protocol_version_is_refused(fake, monkeypatch) -> None:
    fake.anonymous_listing = True
    client = McpClient(StreamableHttpTransport(fake.mcp_url, timeout_s=5), 5)
    assert client.connect()["name"] == "oauth-fake"
    assert client.protocol_version == "2025-06-18"
    client.close()
    original = _handler

    def old_server(state):
        handler = original(state)

        class Old(handler):
            def _json(self, status, body, headers=None):
                result = body.get("result") if isinstance(body, dict) else None
                if isinstance(result, dict) and "serverInfo" in result:
                    result["protocolVersion"] = "1999-01-01"
                super()._json(status, body, headers)

        return Old

    server = ThreadingHTTPServer(("127.0.0.1", 0), old_server(fake))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
        with pytest.raises(McpError) as excinfo:
            McpClient(StreamableHttpTransport(url, timeout_s=5), 5).connect()
    finally:
        server.shutdown()
        server.server_close()
    assert excinfo.value.hint == "protocol_version_unsupported"


# ---------------------------------------------------------------------------
# Signing out and removing
# ---------------------------------------------------------------------------


def test_signout_revokes_and_deletes_the_tokens(fake, services, manager) -> None:
    row = _add(services, fake)
    _sign_in(manager, services, row)
    tokens = OAuthStore(services.credentials, row["id"]).load_tokens()
    result = manager.signout(row["id"])
    assert result == {"revoked": True, "removed": True, "cancelled": False}
    assert tokens["refresh_token"] in fake.revoked and tokens["access_token"] in fake.revoked
    assert OAuthStore(services.credentials, row["id"]).load_tokens() is None
    assert manager.status(row["id"])["signed_in"] is False


def test_signout_without_revocation_endpoint_still_deletes(fake, services, manager) -> None:
    fake.revocation = False
    row = _add(services, fake)
    _sign_in(manager, services, row)
    assert manager.signout(row["id"])["revoked"] is False
    assert OAuthStore(services.credentials, row["id"]).load_tokens() is None


def test_remove_revokes_and_deletes_tokens_and_client(fake, services, manager, app_ctx) -> None:
    row = _add(services, fake)
    _sign_in(manager, services, row)
    store = OAuthStore(services.credentials, row["id"])
    refresh = store.load_tokens()["refresh_token"]
    assert store.load_client() is not None
    assert services.mcp.remove("remote") is True
    assert refresh in fake.revoked
    assert store.load_tokens() is None and store.load_client() is None
    root = app_ctx.layout.credentials_dir / "mcp"
    assert not root.exists() or not any(p.is_file() for p in root.rglob("*"))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_oauth_config_round_trip_and_partial_update(fake, services) -> None:
    row = _add(services, fake, scopes="a b a", authorize_params={"audience": "x"})
    view = services.mcp.view(row)
    assert view["auth"]["kind"] == "oauth"
    assert view["auth"]["scopes"] == ["a", "b"]
    assert view["auth"]["authorize_params"] == {"audience": "x"}
    assert view["auth"]["client_secret"] == {"configured": False, "source": None}
    row = services.mcp.update(
        "remote", {"auth": {"kind": "oauth", "client_id": OWN_CLIENT_ID, "redirect_port": 53682}}
    )
    view = services.mcp.view(row)
    assert view["auth"]["scopes"] == ["a", "b"]  # kept
    assert (view["auth"]["client_id"], view["auth"]["redirect_port"]) == (OWN_CLIENT_ID, 53682)
    row = services.mcp.update("remote", {"auth": {"kind": "none"}})
    assert services.mcp.view(row)["auth"] == {"kind": "none"}


@pytest.mark.parametrize(
    "auth",
    [
        {"kind": "oauth", "redirect_port": 80},
        {"kind": "oauth", "authorize_params": {"state": "x"}},
        {"kind": "oauth", "authorize_params": {"code_challenge": "x"}},
        {"kind": "oauth", "scopes": ["bad scope"]},
        {"kind": "oauth", "client_metadata_url": "http://example.com/client.json"},
        {"kind": "oauth", "client_metadata_url": "https://example.com/"},
        {"kind": "oauth", "client_id": "x", "client_secret": "keyring://providers/x"},
    ],
)
def test_invalid_oauth_configs_are_rejected(services, auth) -> None:
    with pytest.raises(McpConfigError):
        services.mcp.add("bad", url="https://example.com/mcp", auth=auth)


def test_oauth_with_an_authorization_header_is_rejected(services) -> None:
    with pytest.raises(McpConfigError):
        services.mcp.add(
            "bad",
            url="https://example.com/mcp",
            auth={"kind": "oauth"},
            headers={"Authorization": "Bearer x"},
        )


# ---------------------------------------------------------------------------
# Engine protocol
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(services, tmp_path):
    server = EngineServer(services, user_home=tmp_path / "home")
    emitted: list[dict] = []
    server._mcp_auth._emit = emitted.append
    server._mcp_auth.manager._opener = lambda url: True
    server.emitted = emitted
    yield server
    server.close()


_ids = itertools.count(1)


def _call(engine, method: str, params: dict) -> dict:
    response = engine.handle_line(
        json.dumps({"id": f"o{next(_ids)}", "method": method, "params": params})
    )
    assert response is not None
    return response


def test_protocol_sign_in_status_and_sign_out(engine, fake) -> None:
    assert CAPABILITIES["mcp_oauth_v1"] is True
    created = _call(
        engine,
        "mcp.create",
        {"name": "remote", "url": fake.mcp_url, "auth": {"kind": "oauth", "scopes": ["mcp.read"]}},
    )
    assert created["ok"] is True, created
    assert created["result"]["server"]["auth"]["kind"] == "oauth"

    tested = _call(engine, "mcp.test", {"name": "remote"})["result"]["test"]
    assert tested["hint"] == "oauth_required"
    assert tested["oauth"]["available"] is True
    assert tested["oauth"]["client"] == "registration"

    started = _call(engine, "mcp.auth.start", {"name": "remote"})
    assert started["ok"] is True, started
    flow = started["result"]
    assert flow["name"] == "remote" and flow["browser_opened"] is True
    pending = _call(engine, "mcp.auth.status", {"name": "remote"})["result"]
    assert pending["pending"]["flow_id"] == flow["flow_id"]
    assert pending["kind"] == "oauth" and pending["signed_in"] is False

    _browser(flow["authorize_url"])
    names = [e["event"] for e in engine.emitted]
    assert "mcp.auth.progress" in names and names[-1] == "mcp.auth.finished"
    assert engine.emitted[-1]["payload"]["outcome"] == "signed_in"

    status = _call(engine, "mcp.auth.status", {"name": "remote"})["result"]
    assert status["signed_in"] is True and status["scopes"] == ["mcp.read", "mcp.write"]
    assert _call(engine, "mcp.test", {"name": "remote"})["result"]["test"]["ok"] is True

    signed_out = _call(engine, "mcp.auth.signout", {"name": "remote"})["result"]
    assert signed_out["revoked"] is True and signed_out["removed"] is True
    assert _call(engine, "mcp.test", {"name": "remote"})["result"]["test"]["hint"] == (
        "oauth_required"
    )

    text = json.dumps(engine.emitted) + json.dumps(status) + json.dumps(signed_out)
    for token in _tokens(fake):
        assert token not in text


def test_signing_in_connects_a_server_that_was_waiting_for_it(engine, fake, services) -> None:
    created = _call(
        engine, "mcp.create", {"name": "remote", "url": fake.mcp_url, "auth": {"kind": "oauth"}}
    )
    assert created["ok"] is True, created
    row = services.mcp.show("remote")

    def status() -> str:
        return services.mcp.status(row)["status"]

    # Before signing in the background connection fails with oauth_required,
    # which waits for the owner: no timer retries it.
    services.mcp.reconnect("remote")
    assert _wait_for(lambda: status() == "error")
    assert services.mcp.status(row)["connection"]["hint"] == "oauth_required"

    flow = _call(engine, "mcp.auth.start", {"name": "remote"})["result"]
    _browser(flow["authorize_url"])
    assert engine.emitted[-1]["payload"]["outcome"] == "signed_in"
    assert _wait_for(lambda: status() == "connected")


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_protocol_cancel_and_errors(engine, fake) -> None:
    fake.registration = False
    _call(engine, "mcp.create", {"name": "remote", "url": fake.mcp_url, "auth": {"kind": "oauth"}})
    failed = _call(engine, "mcp.auth.start", {"name": "remote"})
    assert failed["ok"] is False
    assert failed["error"]["code"] == "MCP_AUTH_FAILED"
    assert failed["error"]["details"]["hint"] == "oauth_client_required"

    _call(
        engine,
        "mcp.update",
        {"name": "remote", "auth": {"kind": "oauth", "client_id": OWN_CLIENT_ID}},
    )
    started = _call(engine, "mcp.auth.start", {"name": "remote", "open_browser": False})
    assert started["result"]["browser_opened"] is False
    cancelled = _call(engine, "mcp.auth.cancel", {"name": "remote"})["result"]
    assert cancelled == {"name": "remote", "cancelled": True}
    assert engine.emitted[-1]["payload"]["outcome"] == "cancelled"

    _call(engine, "mcp.create", {"name": "plain", "url": fake.mcp_url})
    not_oauth = _call(engine, "mcp.auth.start", {"name": "plain"})
    assert not_oauth["error"]["code"] == "INVALID_PARAMS"
    assert not_oauth["error"]["details"]["hint"] == "oauth_not_configured"
    missing = _call(engine, "mcp.auth.status", {"name": "ghost"})
    assert missing["error"]["code"] == "NOT_FOUND"
    bad = _call(engine, "mcp.auth.start", {"name": "remote", "timeout_s": "soon"})
    assert bad["error"]["code"] == "INVALID_PARAMS"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_add_login_show_and_logout(fake, tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from rinari.cli.main import app
    from rinari.shared.paths import ENV_HOME

    monkeypatch.setenv(ENV_HOME, str(tmp_path / "cli-home"))
    monkeypatch.setenv("RINARI_KEYRING", "0")
    monkeypatch.chdir(tmp_path)

    def browser(url: str) -> bool:
        threading.Thread(target=_browser, args=(url,), daemon=True).start()
        return True

    monkeypatch.setattr("webbrowser.open", browser)
    runner = CliRunner()

    def call(*args: str):
        result = runner.invoke(app, list(args), catch_exceptions=False)
        for token in _tokens(fake):
            assert token not in result.output
        return result

    added = call(
        "mcp", "add", "remote", "--url", fake.mcp_url, "--oauth", "--oauth-scope", "mcp.read"
    )
    assert added.exit_code == 0, added.output
    assert "sign-in:   no" in call("mcp", "show", "remote").output
    login = call("mcp", "login", "remote", "--timeout", "30")
    assert login.exit_code == 0, login.output
    assert "signed in to remote" in login.output
    assert "sign-in:   yes" in call("mcp", "show", "remote").output
    assert call("mcp", "test", "remote").output.startswith("OK")
    logout = call("mcp", "logout", "remote")
    assert logout.exit_code == 0, logout.output
    assert "tokens revoked and deleted" in logout.output
    misuse = call("mcp", "add", "plain", "--url", fake.mcp_url, "--client-id", "x")
    assert misuse.exit_code != 0
