"""OAuth for remote MCP servers (plan de conectores, C1).

The MCP authorization flow (spec 2025-06-18, with the 2025-03-26 fallback):

1. **Discovery.** Protected Resource Metadata (RFC 9728) at
   `/.well-known/oauth-protected-resource<path>` (then the root), asked for
   even without a 401 because some servers answer `initialize` and
   `tools/list` anonymously. If none is published there, an anonymous
   `initialize` reads the `resource_metadata` of the 401 challenge. Without
   resource metadata (Atlassian) the authorization server is the MCP origin.
   Its metadata comes from RFC 8414 or OpenID discovery.
2. **Client.** The owner's own `client_id` (and secret) when the server
   config has one; otherwise dynamic registration (RFC 7591) when the server
   offers it; otherwise a client ID metadata document (CIMD) when the server
   accepts them and a document URL is configured (per server, or
   `RINARI_MCP_CLIENT_METADATA_URL`); otherwise `oauth_client_required`.
3. **Authorization code with PKCE S256, `state` and `resource`** (RFC 8707),
   opened in the owner's default browser. The redirect goes to
   `http://127.0.0.1:<port>/callback` on a listener that only lives for that
   authorization (RFC 8252): a free port, the port of the registered client,
   or the fixed `redirect_port` a "web" client (Google) needs.
4. **Tokens** (access, refresh, expiry, scopes) go to the CredentialStore
   under `mcp/<server id>/oauth`; a registered client under
   `mcp/<server id>/oauth-client`.
5. **Refresh** before expiry and after a 401 (refresh and retry once, in
   `OAuthSession`). When refreshing is refused the transport fails with the
   stable hint `reauth_required`; a server never signed in gives
   `oauth_required`.
6. **Sign out** revokes (RFC 7009) when the server has a revocation endpoint
   and deletes the tokens.

Secrets: no token, code, verifier or client secret is ever part of an error
message, a log line, an event or a view. Authorization-server error codes
are reduced to `[a-z_]` words and response bodies are never echoed.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import socket
import socketserver
import threading
import time
import webbrowser
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

import httpx

from rinari.application.credentials import FILE_SCHEME, KEYRING_SCHEME, CredentialStore
from rinari.shared.errors import RinariError

from .config import (
    AUTH_OAUTH,
    OAuthConfig,
    ServerDraft,
    is_local_host,
    slot_key,
    validate_client_metadata_url,
)
from .http_transport import safe_url
from .protocol import LATEST_PROTOCOL_VERSION
from .transport import TransportError

TOKENS_SLOT = "oauth"
CLIENT_SLOT = "oauth-client"
CALLBACK_PATH = "/callback"
LOOPBACK_HOST = "127.0.0.1"
CLIENT_NAME = "Rinari"
#: Process-wide client ID metadata document URL, when the owner publishes one.
CLIENT_METADATA_ENV = "RINARI_MCP_CLIENT_METADATA_URL"
DEFAULT_FLOW_TIMEOUT_S = 300.0
MIN_FLOW_TIMEOUT_S = 1.0
MAX_FLOW_TIMEOUT_S = 1800.0
HTTP_TIMEOUT_S = 10.0
#: An access token this close to its expiry is refreshed before it is sent.
REFRESH_SKEW_S = 60.0
#: Issuers that only return a refresh token with `access_type=offline` and,
#: for a grant that already exists, `prompt=consent`.
OFFLINE_ACCESS_ISSUERS = frozenset({"https://accounts.google.com"})

_ERROR_WORD_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_CHALLENGE_PARAM_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(?:"([^"]*)"|([^\s,]+))')

HttpFactory = Callable[[], httpx.Client]


class OAuthError(Exception):
    """An OAuth failure with a stable code and hint (never a secret inside)."""

    def __init__(self, code: str, message: str, hint: str) -> None:
        super().__init__(message)
        # OAUTH_DISCOVERY_FAILED | OAUTH_CLIENT_REQUIRED | OAUTH_REGISTRATION_FAILED
        # | OAUTH_PORT_IN_USE | OAUTH_DENIED | OAUTH_TOKEN_REJECTED | OAUTH_SERVER_ERROR
        # | OAUTH_UNREACHABLE | OAUTH_TIMEOUT | OAUTH_CANCELLED | OAUTH_REAUTH_REQUIRED
        # | OAUTH_SECRET_MISSING | OAUTH_STORE_FAILED
        self.code = code
        self.message = message
        self.hint = hint

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "hint": self.hint}


# -- helpers --------------------------------------------------------------------------


def default_http_factory(home: Path | str | None = None) -> HttpFactory:
    """httpx clients with the owner's network settings (proxy, certificates)."""

    def make() -> httpx.Client:
        from rinari.net import client as net_client

        return net_client(
            home,
            follow_redirects=False,
            timeout=httpx.Timeout(HTTP_TIMEOUT_S, connect=HTTP_TIMEOUT_S),
        )

    return make


def _secure_url(url: Any) -> str | None:
    """`url` when it is https (or http to a loopback host), else None."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.hostname or parts.username or parts.password:
        return None
    if parts.scheme == "https" or (parts.scheme == "http" and is_local_host(url)):
        return url
    return None


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), "", "", ""))


def _same_origin(a: str, b: str) -> bool:
    try:
        return _origin(a) == _origin(b)
    except ValueError:
        return False


def canonical_resource(url: str) -> str:
    """The MCP server URL as an RFC 8707 resource: no query, fragment or final '/'."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def parse_challenge(header: str | None) -> dict[str, str]:
    """Parameters of the `Bearer` challenge in a `WWW-Authenticate` header."""
    if not header:
        return {}
    match = re.search(r"\bbearer\b", header, re.I)
    if match is None:
        return {}
    params: dict[str, str] = {}
    for item in _CHALLENGE_PARAM_RE.finditer(header[match.end() :]):
        value = item.group(2) if item.group(2) is not None else item.group(3)
        params.setdefault(item.group(1).lower(), value)
    return params


def _error_word(value: Any) -> str | None:
    if isinstance(value, str) and _ERROR_WORD_RE.match(value):
        return value
    return None


def _response_error(response: httpx.Response) -> str:
    """`, <oauth error code>` from a JSON error body, or ''. Never the body."""
    try:
        data = response.json()
    except ValueError:
        return ""
    word = _error_word(data.get("error")) if isinstance(data, dict) else None
    return f", {word}" if word else ""


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str) and v)


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


# -- discovery ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthMetadata:
    """What discovery found: the resource and its authorization server."""

    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    revocation_endpoint: str | None = None
    client_id_metadata_document_supported: bool = False
    token_endpoint_auth_methods: tuple[str, ...] = ()
    #: `scopes_supported` of the protected resource metadata.
    scopes_supported: tuple[str, ...] = ()
    #: `scope` of the 401 challenge, when discovery saw one.
    challenge_scopes: tuple[str, ...] = ()
    #: Whether the server publishes protected resource metadata.
    resource_metadata: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "resource": self.resource,
            "resource_metadata": self.resource_metadata,
            "registration": self.registration_endpoint is not None,
            "client_metadata_document": self.client_id_metadata_document_supported,
            "revocation": self.revocation_endpoint is not None,
        }


def _get_json(http: httpx.Client, url: str) -> dict[str, Any] | None:
    try:
        response = http.get(url, headers={"Accept": "application/json"}, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise OAuthError(
            "OAUTH_UNREACHABLE", f"cannot reach {safe_url(url)}", "check_url_or_network"
        ) from exc
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _first_json(http: httpx.Client, urls: Iterable[str]) -> dict[str, Any] | None:
    for url in dict.fromkeys(urls):
        data = _get_json(http, url)
        if data is not None:
            return data
    return None


def resource_metadata_urls(url: str) -> list[str]:
    """RFC 9728 well-known URLs for an MCP URL: path-suffixed first, then the root."""
    parts = urlsplit(url)
    origin = _origin(url)
    path = parts.path.rstrip("/")
    urls = [f"{origin}/.well-known/oauth-protected-resource{path}"] if path else []
    urls.append(f"{origin}/.well-known/oauth-protected-resource")
    return urls


def authorization_server_metadata_urls(issuer: str) -> list[str]:
    """RFC 8414 and OpenID discovery URLs for an issuer (with or without a path)."""
    origin = _origin(issuer)
    path = urlsplit(issuer).path.rstrip("/")
    if path:
        return [
            f"{origin}/.well-known/oauth-authorization-server{path}",
            f"{origin}/.well-known/openid-configuration{path}",
            f"{origin}{path}/.well-known/openid-configuration",
        ]
    return [
        f"{origin}/.well-known/oauth-authorization-server",
        f"{origin}/.well-known/openid-configuration",
    ]


def anonymous_challenge(url: str, http: httpx.Client) -> dict[str, str]:
    """The `Bearer` challenge an anonymous `initialize` gets (empty without a 401)."""
    body = {
        "jsonrpc": "2.0",
        "id": "rinari-oauth-discovery",
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "rinari", "version": "1.0.0"},
        },
    }
    try:
        response = http.post(
            url,
            json=body,
            headers={"Accept": "application/json, text/event-stream"},
        )
    except httpx.HTTPError:
        return {}
    with contextlib.suppress(httpx.HTTPError):
        response.close()
    session = response.headers.get("mcp-session-id")
    if session and response.status_code < 300:
        # Answered anonymously: end the session this probe opened.
        with contextlib.suppress(httpx.HTTPError):
            http.delete(url, headers={"Mcp-Session-Id": session}).close()
    if response.status_code not in (401, 403):
        return {}
    return parse_challenge(response.headers.get("www-authenticate"))


def discover(
    url: str,
    http: httpx.Client,
    *,
    challenge: Mapping[str, str] | None = None,
    probe: bool = True,
) -> AuthMetadata:
    """Find the authorization server of the MCP server at `url`.

    `challenge` is the parsed `WWW-Authenticate` of a 401 when the caller has
    one. Without it, the well-known URLs are asked first and, with `probe`,
    an anonymous `initialize` reads the challenge.
    """
    challenge = dict(challenge or {})
    resource = canonical_resource(url)
    candidates: list[str] = []
    named = _secure_url(challenge.get("resource_metadata"))
    if named:
        candidates.append(named)
    candidates.extend(resource_metadata_urls(url))
    prm = _first_json(http, candidates)
    if prm is None and probe and not challenge:
        challenge = anonymous_challenge(url, http)
        named = _secure_url(challenge.get("resource_metadata"))
        if named:
            prm = _get_json(http, named)
    challenge_scopes = tuple((challenge.get("scope") or "").split())
    scopes_supported: tuple[str, ...] = ()
    if prm is not None:
        issuer = next(
            (u for u in _strings(prm.get("authorization_servers")) if _secure_url(u)), None
        )
        if issuer is None:
            raise OAuthError(
                "OAUTH_DISCOVERY_FAILED",
                f"the resource metadata of {safe_url(url)} names no usable authorization server",
                "oauth_metadata_invalid",
            )
        declared = prm.get("resource")
        if isinstance(declared, str) and declared:
            if not _same_origin(declared, url):
                raise OAuthError(
                    "OAUTH_DISCOVERY_FAILED",
                    f"the resource metadata of {safe_url(url)} describes another server",
                    "oauth_metadata_invalid",
                )
            resource = declared
        scopes_supported = _strings(prm.get("scopes_supported"))
    else:
        # 2025-03-26: without resource metadata the MCP origin is the server.
        issuer = _origin(url)
    asm = _first_json(http, authorization_server_metadata_urls(issuer))
    if asm is None:
        raise OAuthError(
            "OAUTH_DISCOVERY_FAILED",
            f"{safe_url(issuer)} publishes no OAuth authorization server metadata",
            "oauth_metadata_missing",
        )
    declared_issuer = asm.get("issuer")
    if isinstance(declared_issuer, str) and declared_issuer.rstrip("/") != issuer.rstrip("/"):
        raise OAuthError(
            "OAUTH_DISCOVERY_FAILED",
            f"the authorization server metadata of {safe_url(issuer)} names another issuer",
            "oauth_metadata_invalid",
        )
    authorization_endpoint = _secure_url(asm.get("authorization_endpoint"))
    token_endpoint = _secure_url(asm.get("token_endpoint"))
    if authorization_endpoint is None or token_endpoint is None:
        raise OAuthError(
            "OAUTH_DISCOVERY_FAILED",
            f"{safe_url(issuer)} has no usable https authorization or token endpoint",
            "oauth_metadata_invalid",
        )
    methods = asm.get("code_challenge_methods_supported")
    if isinstance(methods, list) and "S256" not in methods:
        raise OAuthError(
            "OAUTH_DISCOVERY_FAILED",
            f"{safe_url(issuer)} does not support PKCE with S256",
            "oauth_pkce_unsupported",
        )
    return AuthMetadata(
        resource=resource,
        issuer=issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        registration_endpoint=_secure_url(asm.get("registration_endpoint")),
        revocation_endpoint=_secure_url(asm.get("revocation_endpoint")),
        client_id_metadata_document_supported=asm.get("client_id_metadata_document_supported")
        is True,
        token_endpoint_auth_methods=_strings(asm.get("token_endpoint_auth_methods_supported")),
        scopes_supported=scopes_supported,
        challenge_scopes=challenge_scopes,
        resource_metadata=prm is not None,
    )


# -- clients and tokens ---------------------------------------------------------------


@dataclass(slots=True)
class OAuthClient:
    client_id: str
    #: configured (the owner's own) | registered (RFC 7591) | metadata_document (CIMD)
    source: str
    client_secret: str | None = None
    #: none | client_secret_basic | client_secret_post
    auth_method: str = "none"
    redirect_uris: tuple[str, ...] = ()


def _secret_auth_method(supported: tuple[str, ...]) -> str:
    if supported and "client_secret_basic" not in supported and "client_secret_post" in supported:
        return "client_secret_post"
    return "client_secret_basic"


def register_client(
    http: httpx.Client, metadata: AuthMetadata, redirect_uri: str, scopes: tuple[str, ...] = ()
) -> OAuthClient:
    """Dynamic client registration (RFC 7591) as a public native client."""
    assert metadata.registration_endpoint is not None
    body: dict[str, Any] = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "native",
    }
    if scopes:
        body["scope"] = " ".join(scopes)
    try:
        response = http.post(
            metadata.registration_endpoint, json=body, headers={"Accept": "application/json"}
        )
    except httpx.HTTPError as exc:
        raise OAuthError(
            "OAUTH_UNREACHABLE",
            f"cannot reach {safe_url(metadata.registration_endpoint)}",
            "check_url_or_network",
        ) from exc
    if response.status_code not in (200, 201):
        raise OAuthError(
            "OAUTH_REGISTRATION_FAILED",
            f"{safe_url(metadata.registration_endpoint)} refused to register Rinari "
            f"(HTTP {response.status_code}{_response_error(response)}); "
            "add your own OAuth client ID to the server",
            "oauth_registration_rejected",
        )
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("client_id"), str):
        raise OAuthError(
            "OAUTH_REGISTRATION_FAILED",
            f"{safe_url(metadata.registration_endpoint)} answered the registration without a "
            "client_id",
            "oauth_registration_rejected",
        )
    secret = data.get("client_secret") if isinstance(data.get("client_secret"), str) else None
    method = data.get("token_endpoint_auth_method")
    if method not in ("none", "client_secret_basic", "client_secret_post"):
        method = "client_secret_basic" if secret else "none"
    return OAuthClient(
        client_id=data["client_id"],
        source="registered",
        client_secret=secret,
        auth_method=method,
        redirect_uris=_strings(data.get("redirect_uris")) or (redirect_uri,),
    )


def _authenticated(
    client: OAuthClient, form: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    """Form and headers of a token/revocation request with the client's authentication."""
    data = {k: v for k, v in form.items() if v}
    headers = {"Accept": "application/json"}
    if client.auth_method == "client_secret_basic" and client.client_secret:
        pair = f"{quote(client.client_id, safe='')}:{quote(client.client_secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode("ascii")
    else:
        data["client_id"] = client.client_id
        if client.auth_method == "client_secret_post" and client.client_secret:
            data["client_secret"] = client.client_secret
    return data, headers


def token_request(
    http: httpx.Client, endpoint: str, client: OAuthClient, form: Mapping[str, str]
) -> dict[str, Any]:
    """POST to a token endpoint with the client's authentication; the JSON answer."""
    data, headers = _authenticated(client, form)
    try:
        response = http.post(endpoint, data=data, headers=headers)
    except httpx.HTTPError as exc:
        raise OAuthError(
            "OAUTH_UNREACHABLE", f"cannot reach {safe_url(endpoint)}", "check_url_or_network"
        ) from exc
    if response.status_code >= 500 or response.status_code in (408, 429):
        # Passing (busy, rate limited, timed out): never a reason to throw a
        # refresh token away; the connection retries later.
        raise OAuthError(
            "OAUTH_SERVER_ERROR",
            f"{safe_url(endpoint)} failed (HTTP {response.status_code})",
            "rate_limited" if response.status_code == 429 else "server_error",
        )
    if response.status_code != 200:
        raise OAuthError(
            "OAUTH_TOKEN_REJECTED",
            f"{safe_url(endpoint)} rejected the request "
            f"(HTTP {response.status_code}{_response_error(response)})",
            "oauth_token_rejected",
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
        raise OAuthError(
            "OAUTH_TOKEN_REJECTED",
            f"{safe_url(endpoint)} answered without an access token",
            "oauth_token_rejected",
        )
    token_type = payload.get("token_type")
    if isinstance(token_type, str) and token_type.lower() != "bearer":
        raise OAuthError(
            "OAUTH_TOKEN_REJECTED",
            f"{safe_url(endpoint)} issued a {_error_word(token_type.lower()) or 'non-bearer'} "
            "token; only bearer tokens are supported",
            "oauth_token_rejected",
        )
    return payload


def token_record(
    payload: Mapping[str, Any],
    *,
    now: float,
    token_endpoint: str,
    revocation_endpoint: str | None,
    issuer: str,
    resource: str,
    client: OAuthClient,
    requested_scopes: tuple[str, ...] = (),
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """What `mcp/<id>/oauth` keeps: tokens plus how to refresh and revoke them."""
    expires_in = payload.get("expires_in")
    expires_at: float | None = None
    if isinstance(expires_in, int | float) and not isinstance(expires_in, bool) and expires_in > 0:
        expires_at = now + float(expires_in)
    refresh = payload.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        # A refresh answer may omit it: the previous one stays valid.
        same_client = previous is not None and previous.get("client_id") == client.client_id
        refresh = previous.get("refresh_token") if same_client and previous else None
    scope = payload.get("scope")
    if isinstance(scope, str):
        scopes = scope.split()
    elif previous is not None and isinstance(previous.get("scopes"), list):
        scopes = list(previous["scopes"])
    else:
        scopes = list(requested_scopes)
    return {
        "version": 1,
        "access_token": payload["access_token"],
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_at": expires_at,
        "scopes": scopes,
        "obtained_at": now,
        "issuer": issuer,
        "resource": resource,
        "token_endpoint": token_endpoint,
        "revocation_endpoint": revocation_endpoint,
        "client_id": client.client_id,
        "client_source": client.source,
        "client_auth_method": client.auth_method,
    }


class OAuthStore:
    """The CredentialStore entries of one server: tokens and registered client."""

    def __init__(self, credentials: CredentialStore, server_id: str) -> None:
        self._credentials = credentials
        self.server_id = server_id

    def _refs(self, slot: str) -> list[str]:
        key = slot_key(self.server_id, slot)
        refs = [f"{KEYRING_SCHEME}{key}"] if self._credentials.keyring is not None else []
        refs.append(f"{FILE_SCHEME}{key}")
        return refs

    def _load(self, slot: str) -> dict[str, Any] | None:
        for ref in self._refs(slot):
            try:
                if not self._credentials.exists(ref):
                    continue
                data = json.loads(self._credentials.resolve(ref))
            except (RinariError, OSError, ValueError):
                continue
            if isinstance(data, dict):
                return data
        return None

    def _save(self, slot: str, data: Mapping[str, Any]) -> None:
        try:
            self._credentials.store_secret(
                slot_key(self.server_id, slot), json.dumps(data, sort_keys=True)
            )
        except RinariError as exc:
            raise OAuthError(
                "OAUTH_STORE_FAILED",
                f"cannot keep the sign-in in the credential store: {exc.message}",
                "credential_store_unavailable",
            ) from exc

    def _delete(self, slot: str) -> bool:
        removed = False
        for ref in self._refs(slot):
            with contextlib.suppress(RinariError, OSError):
                removed = self._credentials.delete(ref) or removed
        return removed

    def load_tokens(self) -> dict[str, Any] | None:
        tokens = self._load(TOKENS_SLOT)
        return tokens if tokens and isinstance(tokens.get("access_token"), str) else None

    def save_tokens(self, tokens: Mapping[str, Any]) -> None:
        self._save(TOKENS_SLOT, tokens)

    def delete_tokens(self) -> bool:
        return self._delete(TOKENS_SLOT)

    def load_client(self) -> dict[str, Any] | None:
        client = self._load(CLIENT_SLOT)
        return client if client and isinstance(client.get("client_id"), str) else None

    def save_client(self, client: Mapping[str, Any]) -> None:
        self._save(CLIENT_SLOT, client)

    def delete_client(self) -> bool:
        return self._delete(CLIENT_SLOT)


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _server_lock(server_id: str) -> threading.RLock:
    """One lock per server in the process: refreshes never race each other."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(server_id, threading.RLock())


SecretLoader = Callable[[], str | None]


def _client_from_record(
    store: OAuthStore, tokens: Mapping[str, Any], client_secret: SecretLoader | None
) -> OAuthClient:
    source = str(tokens.get("client_source") or "configured")
    secret: str | None = None
    if source == "registered":
        saved = store.load_client() or {}
        secret = saved.get("client_secret") if isinstance(saved.get("client_secret"), str) else None
    elif source == "configured" and client_secret is not None:
        try:
            secret = client_secret()
        except RinariError as exc:
            raise OAuthError(
                "OAUTH_SECRET_MISSING",
                f"the OAuth client secret is not available: {exc.message}",
                "secret_missing",
            ) from exc
    method = str(tokens.get("client_auth_method") or "none")
    if method != "none" and not secret:
        method = "none"
    return OAuthClient(
        client_id=str(tokens.get("client_id") or ""),
        source=source,
        client_secret=secret,
        auth_method=method,
    )


# -- the transport side ---------------------------------------------------------------


def _reauth_error(
    message: str = "the sign-in for this server expired; sign in again",
) -> TransportError:
    return TransportError("TRANSPORT_AUTH", message, http_status=401, hint="reauth_required")


class OAuthSession:
    """Bearer tokens for one server's HTTP transport.

    `authorization()` gives the header to send (refreshing a token about to
    expire); after a 401, `recover(sent)` refreshes and says whether one
    retry makes sense. Tokens are cached in memory and re-read from the
    store under the server lock before any refresh, so two connections never
    spend the same rotating refresh token.
    """

    def __init__(
        self,
        store: OAuthStore,
        *,
        client_secret: SecretLoader | None = None,
        http_factory: HttpFactory | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._client_secret = client_secret
        self._http_factory = http_factory or default_http_factory()
        self._clock = clock
        self._tokens: dict[str, Any] | None = None
        self._loaded = False
        self._had_tokens = False

    # -- state --------------------------------------------------------------------------

    def _current(self) -> dict[str, Any] | None:
        if not self._loaded:
            self._tokens = self._store.load_tokens()
            self._loaded = True
        if self._tokens is not None:
            self._had_tokens = True
        return self._tokens

    def _expires_at(self, tokens: Mapping[str, Any]) -> float | None:
        value = tokens.get("expires_at")
        return float(value) if isinstance(value, int | float) else None

    def _expired(self, tokens: Mapping[str, Any]) -> bool:
        expires_at = self._expires_at(tokens)
        return expires_at is not None and self._clock() >= expires_at

    def _expiring(self, tokens: Mapping[str, Any]) -> bool:
        expires_at = self._expires_at(tokens)
        return expires_at is not None and self._clock() >= expires_at - REFRESH_SKEW_S

    @property
    def signed_in(self) -> bool:
        return self._current() is not None

    def failure_hint(self) -> str:
        """`reauth_required` once signed in, `oauth_required` before."""
        return "reauth_required" if self._had_tokens else "oauth_required"

    # -- headers -------------------------------------------------------------------------

    def authorization(self) -> str | None:
        """The `Authorization` value to send, or None when never signed in."""
        tokens = self._current()
        if tokens is None:
            return None
        if self._expiring(tokens):
            tokens = self._refresh_before_expiry(tokens)
        return f"Bearer {tokens['access_token']}"

    def _refresh_before_expiry(self, tokens: dict[str, Any]) -> dict[str, Any]:
        with _server_lock(self._store.server_id):
            fresh = self._store.load_tokens()
            if fresh is None:
                self._tokens = None
                raise TransportError(
                    "TRANSPORT_AUTH",
                    "this server is not signed in",
                    http_status=401,
                    hint="oauth_required",
                )
            self._tokens = fresh
            if not self._expiring(fresh):
                return fresh  # another connection refreshed it meanwhile
            if not fresh.get("refresh_token"):
                if self._expired(fresh):
                    raise _reauth_error()
                return fresh  # still valid for a little while
            try:
                return self._refresh(fresh)
            except OAuthError as exc:
                if not self._expired(fresh):
                    return self._tokens or fresh  # use it while it lasts
                if exc.hint == "reauth_required":
                    raise _reauth_error(exc.message) from exc
                raise TransportError("TRANSPORT_UNREACHABLE", exc.message, hint=exc.hint) from exc

    def recover(self, sent: str | None) -> bool:
        """After a 401: refresh the token; True when one retry makes sense."""
        with _server_lock(self._store.server_id):
            tokens = self._store.load_tokens()
            self._tokens, self._loaded = tokens, True
            if tokens is None:
                return False
            self._had_tokens = True
            current = f"Bearer {tokens['access_token']}"
            if current != sent and not self._expired(tokens):
                # Another connection refreshed it, or the sign-in happened
                # after this one was made: retry with it before refreshing.
                return True
            if not tokens.get("refresh_token"):
                self._mark_reauth(tokens)
                return False
            try:
                self._refresh(tokens)
            except OAuthError as exc:
                if exc.hint == "reauth_required":
                    return False
                raise TransportError("TRANSPORT_UNREACHABLE", exc.message, hint=exc.hint) from exc
            return True

    # -- refresh ---------------------------------------------------------------------

    def _mark_reauth(self, tokens: Mapping[str, Any]) -> None:
        marked = {**tokens, "refresh_token": None, "reauth_required": True}
        with contextlib.suppress(OAuthError):
            self._store.save_tokens(marked)
        self._tokens = marked

    def _refresh(self, tokens: dict[str, Any]) -> dict[str, Any]:
        """Spend the refresh token (caller holds the server lock)."""
        endpoint = _secure_url(tokens.get("token_endpoint"))
        if endpoint is None:
            self._mark_reauth(tokens)
            raise OAuthError(
                "OAUTH_REAUTH_REQUIRED", "the saved sign-in cannot be renewed", "reauth_required"
            )
        client = _client_from_record(self._store, tokens, self._client_secret)
        form = {
            "grant_type": "refresh_token",
            "refresh_token": str(tokens.get("refresh_token") or ""),
            "resource": str(tokens.get("resource") or ""),
        }
        try:
            with self._http_factory() as http:
                payload = token_request(http, endpoint, client, form)
        except OAuthError as exc:
            if exc.code == "OAUTH_TOKEN_REJECTED":
                self._mark_reauth(tokens)
                raise OAuthError(
                    "OAUTH_REAUTH_REQUIRED",
                    "the authorization server no longer accepts the saved sign-in; sign in again",
                    "reauth_required",
                ) from exc
            raise
        record = token_record(
            payload,
            now=self._clock(),
            token_endpoint=endpoint,
            revocation_endpoint=tokens.get("revocation_endpoint"),
            issuer=str(tokens.get("issuer") or ""),
            resource=str(tokens.get("resource") or ""),
            client=client,
            previous=tokens,
        )
        self._store.save_tokens(record)
        self._tokens = record
        return record


def _secret_loader(oauth: OAuthConfig, resolve: Callable[[str], str] | None) -> SecretLoader | None:
    slot = oauth.client_secret
    if slot is None:
        return None

    def load() -> str | None:
        if slot.literal is not None:
            return slot.literal
        if slot.ref is None or resolve is None:
            return None
        return resolve(slot.ref)

    return load


def session_for(
    credentials: CredentialStore,
    server_id: str,
    draft: ServerDraft,
    *,
    home: Path | str | None = None,
    http_factory: HttpFactory | None = None,
) -> OAuthSession | None:
    """The OAuth session of a saved `oauth` server's transport (None otherwise)."""
    if draft.auth_kind != AUTH_OAUTH or not server_id:
        return None
    if _secure_url(draft.url) is None:
        return None  # a row saved before https was required: its token never goes in clear
    return OAuthSession(
        OAuthStore(credentials, server_id),
        client_secret=_secret_loader(draft.oauth or OAuthConfig(), credentials.resolve),
        http_factory=http_factory or default_http_factory(home),
    )


def revoke(
    store: OAuthStore,
    tokens: Mapping[str, Any],
    *,
    client_secret: SecretLoader | None,
    http_factory: HttpFactory,
) -> bool:
    """RFC 7009 revocation of the refresh and access tokens (best effort)."""
    endpoint = _secure_url(tokens.get("revocation_endpoint"))
    if endpoint is None:
        return False
    try:
        client = _client_from_record(store, tokens, client_secret)
    except OAuthError:
        return False
    revoked = False
    with http_factory() as http:
        for hint in ("refresh_token", "access_token"):
            value = tokens.get(hint)
            if not isinstance(value, str) or not value:
                continue
            data, headers = _authenticated(client, {"token": value, "token_type_hint": hint})
            try:
                response = http.post(endpoint, data=data, headers=headers)
            except httpx.HTTPError:
                continue
            revoked = revoked or response.status_code == 200
    return revoked


def forget_server(
    credentials: CredentialStore,
    server_id: str,
    draft: ServerDraft,
    *,
    home: Path | str | None = None,
    http_factory: HttpFactory | None = None,
) -> None:
    """A removed server: revoke its tokens, delete them and its registered client."""
    store = OAuthStore(credentials, server_id)
    tokens = store.load_tokens()
    if tokens is not None:
        with contextlib.suppress(Exception):  # revocation never blocks a removal
            revoke(
                store,
                tokens,
                client_secret=_secret_loader(draft.oauth or OAuthConfig(), credentials.resolve),
                http_factory=http_factory or default_http_factory(home),
            )
        store.delete_tokens()
    store.delete_client()


def describe_support(
    url: str,
    http: httpx.Client,
    *,
    oauth: OAuthConfig | None = None,
    client_metadata_url: str | None = None,
) -> dict[str, Any] | None:
    """What «Probar» says about OAuth at `url` (None when it has none).

    `client` tells the UI what signing in needs: `configured` (the owner's
    client is set), `registration`, `metadata_document`, or `required` (the
    owner must add a client ID).
    """
    try:
        metadata = discover(url, http, probe=False)
    except OAuthError:
        return None
    oauth = oauth or OAuthConfig()
    cimd = oauth.client_metadata_url or client_metadata_url
    if oauth.client_id:
        client = "configured"
    elif metadata.registration_endpoint:
        client = "registration"
    elif metadata.client_id_metadata_document_supported and cimd:
        client = "metadata_document"
    else:
        client = "required"
    return {"available": True, "client": client, **metadata.summary()}


def env_client_metadata_url(env: Mapping[str, str] | None = None) -> str | None:
    value = (env if env is not None else os.environ).get(CLIENT_METADATA_ENV)
    if not value:
        return None
    try:
        return validate_client_metadata_url(value)
    except ValueError:
        return None


# -- the authorization flow ---------------------------------------------------------


class _CallbackServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """The loopback listener of one authorization (127.0.0.1 only)."""

    # A busy port must fail. On POSIX, SO_REUSEADDR never shares a listening
    # port but lets a port be bound again while old connections sit in
    # TIME_WAIT (the next sign-in reuses the registered port); on Windows it
    # would share the port, so there the socket is exclusive instead.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True
    block_on_close = False

    def __init__(self, port: int) -> None:
        self.on_callback: Callable[[dict[str, str]], tuple[int, str]] = lambda _: (
            503,
            "Rinari is not waiting for a sign-in.",
        )
        super().__init__((LOOPBACK_HOST, port), _CallbackHandler)

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer
    timeout = 15
    server_version = "Rinari"
    sys_version = ""

    def log_message(self, *args: Any) -> None:
        return  # the query carries the code and the state: never logged

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path != CALLBACK_PATH:
            self._page(404, "Not found.")
            return
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        try:
            status, text = self.server.on_callback(query)
        except Exception:  # never leave the browser hanging
            status, text = 500, "Rinari could not finish the sign-in. Return to Rinari."
        self._page(status, text)

    def _page(self, status: int, text: str) -> None:
        body = (
            "<!doctype html><html><head><meta charset='utf-8'><title>Rinari</title></head>"
            "<body style='font-family:system-ui,sans-serif;margin:3rem'>"
            f"<p>{html.escape(text)}</p></body></html>"
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Listener:
    def __init__(self, server: _CallbackServer) -> None:
        self.server = server
        self.port = int(server.server_address[1])
        self.redirect_uri = f"http://{LOOPBACK_HOST}:{self.port}{CALLBACK_PATH}"
        self._thread: threading.Thread | None = None
        self._closed = False

    @staticmethod
    def bind(port: int) -> _Listener | None:
        try:
            return _Listener(_CallbackServer(port))
        except OSError:
            return None

    def serve(self) -> None:
        self._thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name=f"rinari-oauth-callback-{self.port}",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:
            self.server.shutdown()
        self.server.server_close()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@dataclass(slots=True)
class _Flow:
    flow_id: str
    server_id: str
    name: str
    authorize_url: str
    state: str
    verifier: str
    metadata: AuthMetadata
    client: OAuthClient
    scopes: tuple[str, ...]
    listener: _Listener
    started_at: float
    timeout_s: float
    previous: dict[str, Any] | None = None
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    claimed: bool = False
    outcome: str | None = None
    error: OAuthError | None = None


EventSink = Callable[[str, dict[str, Any]], None]


class OAuthManager:
    """Authorizations in progress, one per server, plus sign-in status and sign out.

    `start` discovers, picks the client, opens the loopback listener and
    returns the authorization URL at once; the callback, the code exchange
    and the end (`signed_in`, `failed`, `cancelled`, `timed_out`) happen on
    the listener's threads and are reported through `on_event`
    (`mcp.auth.progress`, `mcp.auth.finished`).
    """

    def __init__(
        self,
        credentials: CredentialStore,
        *,
        home: Path | str | None = None,
        http_factory: HttpFactory | None = None,
        opener: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.time,
        on_event: EventSink | None = None,
        client_metadata_url: str | None = None,
    ) -> None:
        self._credentials = credentials
        self._http_factory = http_factory or default_http_factory(home)
        self._opener = opener or webbrowser.open
        self._clock = clock
        self._on_event = on_event or (lambda _name, _payload: None)
        self._client_metadata_url = client_metadata_url or env_client_metadata_url()
        self._lock = threading.Lock()
        self._flows: dict[str, _Flow] = {}
        self._last: dict[str, dict[str, Any]] = {}
        # Sign-ins still discovering or registering (no listener yet), and
        # those cancelled meanwhile: they never reach the browser.
        self._starting: dict[str, str] = {}
        self._cancelled_starts: set[str] = set()

    @property
    def client_metadata_url(self) -> str | None:
        return self._client_metadata_url

    # -- start ---------------------------------------------------------------------------

    def start(
        self,
        server_id: str,
        name: str,
        url: str,
        oauth: OAuthConfig | None = None,
        *,
        client_secret: str | None = None,
        open_browser: bool = True,
        timeout_s: float = DEFAULT_FLOW_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Begin signing in to `url`; raises OAuthError before the browser step.

        A `cancel` while it still discovers or registers wins: the listener
        closes, nothing opens and this raises `OAUTH_CANCELLED`.
        """
        oauth = oauth or OAuthConfig()
        if _secure_url(url) is None:
            raise OAuthError(
                "OAUTH_INSECURE_URL",
                f"{safe_url(url)} is not https; OAuth needs https (http only on this computer)",
                "https_required",
            )
        timeout_s = min(max(float(timeout_s), MIN_FLOW_TIMEOUT_S), MAX_FLOW_TIMEOUT_S)
        self.cancel(server_id)  # a new sign-in replaces one in progress
        flow_id = f"oauth_{secrets.token_hex(8)}"
        with self._lock:
            self._starting[server_id] = flow_id
        try:
            return self._start(
                server_id, name, url, oauth, flow_id, client_secret, open_browser, timeout_s
            )
        finally:
            with self._lock:
                if self._starting.get(server_id) == flow_id:
                    del self._starting[server_id]
                self._cancelled_starts.discard(flow_id)

    def _start(
        self,
        server_id: str,
        name: str,
        url: str,
        oauth: OAuthConfig,
        flow_id: str,
        client_secret: str | None,
        open_browser: bool,
        timeout_s: float,
    ) -> dict[str, Any]:
        store = OAuthStore(self._credentials, server_id)
        self._emit_progress(name, flow_id, "discovering")
        listener: _Listener | None = None
        try:
            with self._http_factory() as http:
                metadata = discover(url, http)
                scopes = (
                    tuple(oauth.scopes) or metadata.challenge_scopes or metadata.scopes_supported
                )
                client, listener = self._client(
                    http, store, metadata, oauth, client_secret, scopes, name, flow_id
                )
        except BaseException:
            if listener is not None:
                listener.close()
            raise
        previous = store.load_tokens()
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": listener.redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": metadata.resource,
        }
        if scopes:
            params["scope"] = " ".join(scopes)
        if metadata.issuer.rstrip("/") in OFFLINE_ACCESS_ISSUERS:
            params["access_type"] = "offline"
            same_client = previous is not None and previous.get("client_id") == client.client_id
            if not (same_client and previous and previous.get("refresh_token")):
                params["prompt"] = "consent"  # without it an existing grant gets no refresh token
        params.update(oauth.authorize_params)
        endpoint = metadata.authorization_endpoint
        authorize_url = f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(params)}"
        flow = _Flow(
            flow_id=flow_id,
            server_id=server_id,
            name=name,
            authorize_url=authorize_url,
            state=state,
            verifier=verifier,
            metadata=metadata,
            client=client,
            scopes=scopes,
            listener=listener,
            started_at=self._clock(),
            timeout_s=timeout_s,
            previous=previous,
        )
        listener.server.on_callback = lambda query: self._on_callback(flow, query)
        with self._lock:
            cancelled = flow_id in self._cancelled_starts
            if not cancelled:
                self._flows[server_id] = flow
        if cancelled:
            # Cancelled while discovering: finish it here, before the browser.
            error = OAuthError("OAUTH_CANCELLED", "sign-in cancelled", "oauth_cancelled")
            self._finish(flow, "cancelled", error)
            raise error
        listener.serve()
        threading.Thread(
            target=self._watch, args=(flow,), name=f"rinari-oauth-wait-{flow_id}", daemon=True
        ).start()
        self._emit_progress(name, flow_id, "waiting_for_browser")
        opened = False
        if open_browser:
            with contextlib.suppress(Exception):
                opened = bool(self._opener(authorize_url))
        return {
            "flow_id": flow_id,
            "authorize_url": authorize_url,
            "redirect_uri": listener.redirect_uri,
            "browser_opened": opened,
            "expires_at": _iso(flow.started_at + timeout_s),
            "client": client.source,
            **metadata.summary(),
        }

    def _client(
        self,
        http: httpx.Client,
        store: OAuthStore,
        metadata: AuthMetadata,
        oauth: OAuthConfig,
        client_secret: str | None,
        scopes: tuple[str, ...],
        name: str,
        flow_id: str,
    ) -> tuple[OAuthClient, _Listener]:
        fixed = oauth.redirect_port

        def bind(port: int) -> _Listener:
            listener = _Listener.bind(port)
            if listener is None:
                raise OAuthError(
                    "OAUTH_PORT_IN_USE",
                    f"port {port} on {LOOPBACK_HOST} is in use; close what uses it or choose "
                    "another redirect port",
                    "redirect_port_busy",
                )
            return listener

        if oauth.client_id:
            method = (
                _secret_auth_method(metadata.token_endpoint_auth_methods)
                if client_secret
                else "none"
            )
            client = OAuthClient(oauth.client_id, "configured", client_secret, method)
            return client, bind(fixed or 0)
        if metadata.registration_endpoint:
            saved = store.load_client()
            if saved is not None and saved.get("issuer") == metadata.issuer:
                uris = _strings(saved.get("redirect_uris"))
                port = fixed or next((urlsplit(u).port for u in uris if urlsplit(u).port), None)
                listener = _Listener.bind(port) if port else None
                if listener is not None and listener.redirect_uri in uris:
                    return self._saved_client(saved), listener
                if listener is not None:
                    listener.close()
            listener = bind(fixed or 0)
            try:
                self._emit_progress(name, flow_id, "registering")
                client = register_client(http, metadata, listener.redirect_uri, scopes)
                store.save_client(
                    {
                        "issuer": metadata.issuer,
                        "client_id": client.client_id,
                        "client_secret": client.client_secret,
                        "token_endpoint_auth_method": client.auth_method,
                        "redirect_uris": list(client.redirect_uris),
                    }
                )
            except BaseException:
                listener.close()
                raise
            return client, listener
        cimd = oauth.client_metadata_url or self._client_metadata_url
        if metadata.client_id_metadata_document_supported and cimd:
            return OAuthClient(cimd, "metadata_document"), bind(fixed or 0)
        raise OAuthError(
            "OAUTH_CLIENT_REQUIRED",
            f"{safe_url(metadata.issuer)} does not register clients by itself; add your own "
            "OAuth client ID (and secret, if it has one) to this server",
            "oauth_client_required",
        )

    @staticmethod
    def _saved_client(saved: Mapping[str, Any]) -> OAuthClient:
        secret = saved.get("client_secret") if isinstance(saved.get("client_secret"), str) else None
        method = saved.get("token_endpoint_auth_method")
        if method not in ("none", "client_secret_basic", "client_secret_post"):
            method = "client_secret_basic" if secret else "none"
        return OAuthClient(
            client_id=str(saved["client_id"]),
            source="registered",
            client_secret=secret,
            auth_method=method,
            redirect_uris=_strings(saved.get("redirect_uris")),
        )

    # -- callback ------------------------------------------------------------------------

    def _on_callback(self, flow: _Flow, query: Mapping[str, str]) -> tuple[int, str]:
        state = query.get("state") or ""
        if not hmac.compare_digest(state.encode("utf-8"), flow.state.encode("utf-8")):
            # Not this sign-in: refused, and the real callback can still arrive.
            return 400, "This sign-in link does not belong to the sign-in Rinari is waiting for."
        with flow.lock:
            if flow.claimed or flow.outcome is not None:
                return 400, "This sign-in is already finished. Return to Rinari."
            flow.claimed = True
        if "error" in query:
            word = _error_word(query.get("error")) or "error"
            self._finish(
                flow,
                "failed",
                OAuthError(
                    "OAUTH_DENIED",
                    f"the authorization server did not grant access ({word})",
                    "oauth_denied",
                ),
            )
            return 200, "The sign-in was not completed. You can close this tab."
        code = query.get("code")
        if not code:
            self._finish(
                flow,
                "failed",
                OAuthError("OAUTH_DENIED", "the callback carried no code", "oauth_denied"),
            )
            return 400, "The sign-in was not completed. You can close this tab."
        self._emit_progress(flow.name, flow.flow_id, "exchanging_code")
        try:
            with self._http_factory() as http:
                payload = token_request(
                    http,
                    flow.metadata.token_endpoint,
                    flow.client,
                    {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": flow.listener.redirect_uri,
                        "code_verifier": flow.verifier,
                        "resource": flow.metadata.resource,
                    },
                )
            record = token_record(
                payload,
                now=self._clock(),
                token_endpoint=flow.metadata.token_endpoint,
                revocation_endpoint=flow.metadata.revocation_endpoint,
                issuer=flow.metadata.issuer,
                resource=flow.metadata.resource,
                client=flow.client,
                requested_scopes=flow.scopes,
                previous=flow.previous,
            )
            with flow.lock:
                if flow.outcome is not None:  # cancelled or timed out meanwhile
                    return 409, "This sign-in was cancelled. Return to Rinari."
                with _server_lock(flow.server_id):
                    OAuthStore(self._credentials, flow.server_id).save_tokens(record)
        except OAuthError as exc:
            self._finish(flow, "failed", exc)
            return 200, "The sign-in failed. Return to Rinari for details."
        self._finish(flow, "signed_in", None)
        return 200, "Rinari is connected. You can close this tab."

    def _watch(self, flow: _Flow) -> None:
        if not flow.done.wait(flow.timeout_s):
            self._finish(
                flow,
                "timed_out",
                OAuthError(
                    "OAUTH_TIMEOUT",
                    f"no answer from the browser within {flow.timeout_s:g}s",
                    "oauth_timeout",
                ),
            )

    def _finish(self, flow: _Flow, outcome: str, error: OAuthError | None) -> bool:
        with flow.lock:
            if flow.outcome is not None:
                return False
            flow.outcome, flow.error = outcome, error
        flow.done.set()
        result: dict[str, Any] = {
            "flow_id": flow.flow_id,
            "outcome": outcome,
            "finished_at": _iso(self._clock()),
        }
        if error is not None:
            result["error"] = error.as_dict()
        with self._lock:
            if self._flows.get(flow.server_id) is flow:
                del self._flows[flow.server_id]
            self._last[flow.server_id] = result
        # Callbacks run on their own threads (ThreadingMixIn), never on the
        # serving one, so the listener closes here: its port is free on return.
        flow.listener.close()
        self._on_event("mcp.auth.finished", {"name": flow.name, **result})
        return True

    def _emit_progress(self, name: str, flow_id: str, stage: str) -> None:
        self._on_event("mcp.auth.progress", {"name": name, "flow_id": flow_id, "stage": stage})

    # -- status, cancel, sign out ---------------------------------------------------------

    def cancel(self, server_id: str) -> bool:
        with self._lock:
            flow = self._flows.get(server_id)
            if flow is None:
                starting = self._starting.get(server_id)
                if starting is not None:
                    self._cancelled_starts.add(starting)
                return starting is not None
        return self._finish(
            flow, "cancelled", OAuthError("OAUTH_CANCELLED", "sign-in cancelled", "oauth_cancelled")
        )

    def wait(self, flow_id: str, timeout_s: float | None = None) -> dict[str, Any] | None:
        """Block until the flow ends (CLI); its result, or None while it runs."""
        with self._lock:
            flow = next((f for f in self._flows.values() if f.flow_id == flow_id), None)
            last = next((r for r in self._last.values() if r.get("flow_id") == flow_id), None)
        if flow is not None and flow.done.wait(timeout_s):
            with self._lock:
                last = self._last.get(flow.server_id)
        return last if last is not None and last.get("flow_id") == flow_id else None

    def status(self, server_id: str) -> dict[str, Any]:
        """Sign-in state for views: flags, expiry and scopes, never a token."""
        tokens = OAuthStore(self._credentials, server_id).load_tokens()
        with self._lock:
            flow = self._flows.get(server_id)
            last = self._last.get(server_id)
        expires_at = tokens.get("expires_at") if tokens else None
        expired = isinstance(expires_at, int | float) and self._clock() >= expires_at
        has_refresh = bool(tokens and tokens.get("refresh_token"))
        view: dict[str, Any] = {
            "signed_in": tokens is not None,
            # Refused by the server (flag), or expired with nothing to renew it.
            "reauth_required": bool(
                tokens and (tokens.get("reauth_required") or (expired and not has_refresh))
            ),
            "expires_at": _iso(expires_at) if isinstance(expires_at, int | float) else None,
            "has_refresh_token": has_refresh,
            "scopes": list(tokens.get("scopes") or []) if tokens else [],
            "issuer": tokens.get("issuer") if tokens else None,
            "client": tokens.get("client_source") if tokens else None,
            "pending": None,
            "last": last,
        }
        if flow is not None:
            view["pending"] = {
                "flow_id": flow.flow_id,
                "authorize_url": flow.authorize_url,
                "started_at": _iso(flow.started_at),
                "expires_at": _iso(flow.started_at + flow.timeout_s),
            }
        return view

    def signout(
        self, server_id: str, oauth: OAuthConfig | None = None, *, client_secret: str | None = None
    ) -> dict[str, Any]:
        """Cancel a pending sign-in, revoke the tokens (when possible), delete them."""
        cancelled = self.cancel(server_id)
        store = OAuthStore(self._credentials, server_id)
        with _server_lock(server_id):
            tokens = store.load_tokens()
            revoked = False
            if tokens is not None:
                with contextlib.suppress(Exception):  # sign out never fails on the network
                    revoked = revoke(
                        store,
                        tokens,
                        client_secret=lambda: client_secret,
                        http_factory=self._http_factory,
                    )
            removed = store.delete_tokens()
        with self._lock:
            self._last.pop(server_id, None)
        return {"revoked": revoked, "removed": removed, "cancelled": cancelled}

    def close(self) -> None:
        with self._lock:
            servers = list(self._flows)
        for server_id in servers:
            self.cancel(server_id)


__all__ = [
    "CALLBACK_PATH",
    "CLIENT_METADATA_ENV",
    "CLIENT_SLOT",
    "DEFAULT_FLOW_TIMEOUT_S",
    "TOKENS_SLOT",
    "AuthMetadata",
    "OAuthClient",
    "OAuthError",
    "OAuthManager",
    "OAuthSession",
    "OAuthStore",
    "anonymous_challenge",
    "authorization_server_metadata_urls",
    "canonical_resource",
    "default_http_factory",
    "describe_support",
    "discover",
    "env_client_metadata_url",
    "forget_server",
    "parse_challenge",
    "register_client",
    "resource_metadata_urls",
    "revoke",
    "session_for",
    "token_record",
    "token_request",
]
