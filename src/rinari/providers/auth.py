"""Subscription compatibility authentication, owned exclusively by Engine.

Public-client OAuth; no browser cookies, foreign credential files or agent
runtime delegation. Operations never expose access/refresh/device tokens.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from rinari.providers.catalog import product_for
from rinari.shared.errors import AuthenticationRequiredError, InvalidUsageError
from rinari.shared.locking import file_lock

OPENAI_CLIENT = "app_EMoamEEZ73f0CkXaXp7hrann"
GITHUB_CLIENT = "Ov23li8tweQw6odWQebz"
ISSUER = "https://auth.openai.com"
REDIRECT = "http://localhost:1455/auth/callback"


def claims(token):
    """Account routing hints only, never used to validate authorization."""
    try:
        raw = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, TypeError):
        return {}


def account_id(token):
    value = claims(token)
    return value.get("https://api.openai.com/auth", {}).get("chatgpt_account_id") or value.get(
        "chatgpt_account_id"
    )


def request_json(providers, method, url, **kwargs):
    client = providers._client or httpx.Client(timeout=10, follow_redirects=False)
    try:
        response = client.request(
            method,
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Rinari/0.1.0",
                **kwargs.pop("headers", {}),
            },
            timeout=10,
            follow_redirects=False,
            **kwargs,
        )
        if response.status_code >= 400:
            raise AuthenticationRequiredError(
                f"Authentication service rejected the request (HTTP {response.status_code}). "
                "Reconnect the account."
            )
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (httpx.HTTPError, ValueError) as exc:
        raise AuthenticationRequiredError(
            "Authentication service is unavailable. Retry or reconnect."
        ) from exc
    finally:
        if providers._client is None:
            client.close()


def credential_bundle(tokens, previous=None):
    previous = previous or {}
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        raise AuthenticationRequiredError("Authentication did not return an access token.")
    return {
        "access_token": access,
        "refresh_token": tokens.get("refresh_token", previous.get("refresh_token")),
        "expires_at": time.time() + float(tokens.get("expires_in", 3600)),
        "account_id": account_id(tokens.get("id_token", access)) or previous.get("account_id"),
    }


def token_for(providers, provider, *, rejected_token=None):
    product = product_for(provider)
    if product not in ("chatgpt", "github-copilot"):
        raise InvalidUsageError(
            "Subscription endpoint changed; reconnect using the original product endpoint."
        )
    with file_lock(providers._ctx.home / f"oauth-{provider.id}.lock", timeout=25):
        ref = providers.credential_ref(provider)
        if not ref:
            raise AuthenticationRequiredError("Connect this subscription first.")
        try:
            bundle = json.loads(providers._credentials.resolve(ref))
        except (ValueError, TypeError) as exc:
            raise AuthenticationRequiredError("Reconnect this subscription.") from exc
        if product == "chatgpt" and (
            bundle.get("expires_at", 0) < time.time() + 60
            or rejected_token == bundle.get("access_token")
        ):
            if not bundle.get("refresh_token"):
                raise AuthenticationRequiredError("Subscription session expired. Reconnect.")
            tokens = request_json(
                providers,
                "POST",
                ISSUER + "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": bundle["refresh_token"],
                    "client_id": OPENAI_CLIENT,
                },
            )
            bundle = credential_bundle(tokens, bundle)
            providers.set_oauth(provider.id, bundle)
        return bundle["access_token"]


class ProviderAuthService:
    def __init__(self, providers):
        self.providers = providers
        self.operations = {}
        self.lock = threading.RLock()

    def _view(self, op):
        return {
            k: op.get(k)
            for k in (
                "operation_id",
                "provider_id",
                "status",
                "authorization_url",
                "user_code",
                "expires_at",
                "detail",
            )
        }

    def start(self, ref, method="browser"):
        provider = self.providers.get(ref)
        product = product_for(provider)
        if provider.auth_method != "oauth" or product not in ("chatgpt", "github-copilot"):
            raise InvalidUsageError("This provider does not use subscription login.")
        if method not in ("browser", "device"):
            raise InvalidUsageError("Choose browser or device authentication.")
        self.cancel_provider(provider.id)
        op = {
            "operation_id": secrets.token_hex(16),
            "provider_id": provider.id,
            "product": product,
            "endpoint": provider.endpoint,
            "status": "waiting",
            "detail": "",
            "expires_at": time.time() + 900,
            "next_poll": 0,
        }
        self.operations[op["operation_id"]] = op
        try:
            if product == "chatgpt" and method == "browser":
                self._browser(op)
            elif product == "chatgpt":
                data = request_json(
                    self.providers,
                    "POST",
                    ISSUER + "/api/accounts/deviceauth/usercode",
                    json={"client_id": OPENAI_CLIENT},
                )
                op.update(
                    device_auth_id=data["device_auth_id"],
                    user_code=data["user_code"],
                    interval=max(5, int(data.get("interval", 5))),
                    authorization_url=ISSUER + "/codex/device",
                )
            else:
                data = request_json(
                    self.providers,
                    "POST",
                    "https://github.com/login/device/code",
                    json={"client_id": GITHUB_CLIENT, "scope": "read:user"},
                )
                op.update(
                    device_code=data["device_code"],
                    user_code=data["user_code"],
                    interval=max(5, int(data.get("interval", 5))),
                    expires_at=time.time() + min(900, int(data.get("expires_in", 900))),
                    authorization_url="https://github.com/login/device",
                )
        except Exception:
            op.update(
                status="error",
                detail="Could not start login. For an occupied callback port, choose device login.",
            )
        return self._view(op)

    def _browser(self, op):
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(32)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        owner = self

        class Callback(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(5)
                super().setup()

            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                args = parse_qs(parsed.query)
                valid = parsed.path == "/auth/callback" and secrets.compare_digest(
                    args.get("state", [""])[0], state
                )
                if not valid or op["status"] != "waiting" or time.time() > op["expires_at"]:
                    self.send_response(400)
                    self.end_headers()
                    return
                try:
                    code = args.get("code", [None])[0]
                    if not code:
                        raise ValueError()
                    tokens = request_json(
                        owner.providers,
                        "POST",
                        ISSUER + "/oauth/token",
                        data={
                            "grant_type": "authorization_code",
                            "code": code,
                            "redirect_uri": REDIRECT,
                            "client_id": OPENAI_CLIENT,
                            "code_verifier": verifier,
                        },
                    )
                    owner._complete(op, tokens)
                except Exception:
                    op.update(status="error", detail="Login could not be completed. Start again.")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Return to Rinari to see your connection status.")

        server = HTTPServer(("127.0.0.1", 1455), Callback)
        server.timeout = 0.5
        op["server"] = server
        op["authorization_url"] = (
            ISSUER
            + "/oauth/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": OPENAI_CLIENT,
                    "redirect_uri": REDIRECT,
                    "scope": "openid profile email offline_access",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": state,
                    "id_token_add_organizations": "true",
                    "codex_cli_simplified_flow": "true",
                }
            )
        )

        def listen():
            try:
                while op["status"] == "waiting" and time.time() < op["expires_at"]:
                    server.handle_request()
                if op["status"] == "waiting":
                    op.update(status="expired", detail="Login expired. Start again.")
            finally:
                server.server_close()

        threading.Thread(target=listen, daemon=True, name="rinari-oauth-callback").start()

    def _complete(self, op, tokens):
        with self.lock:
            if op["status"] != "waiting" or time.time() > op["expires_at"]:
                return
            provider = self.providers.get(op["provider_id"])
            if provider.endpoint != op["endpoint"] or product_for(provider) != op["product"]:
                raise InvalidUsageError("Provider changed during login.")
            bundle = credential_bundle(tokens)
            with file_lock(self.providers._ctx.home / f"oauth-{provider.id}.lock"):
                self.providers.set_oauth(provider.id, bundle)
            op.update(status="connected", authorization_url=None, user_code=None)

    def get(self, ref, operation_id=None):
        provider = self.providers.get(ref)
        if operation_id is None:
            status = "disconnected"
            credential = self.providers.credential_ref(provider)
            if credential:
                try:
                    bundle = json.loads(self.providers._credentials.resolve(credential))
                    status = "connected" if bundle.get("access_token") else "needs_auth"
                    if (
                        product_for(provider) == "chatgpt"
                        and bundle.get("expires_at", 0) < time.time()
                        and not bundle.get("refresh_token")
                    ):
                        status = "needs_auth"
                except Exception:
                    status = "needs_auth"
            return {
                "operation_id": None,
                "provider_id": provider.id,
                "status": status,
                "authorization_url": None,
                "user_code": None,
                "expires_at": None,
                "detail": "",
            }
        op = self.operations.get(operation_id)
        if not op or op["provider_id"] != provider.id:
            raise InvalidUsageError("Unknown login operation. Start login again.")
        with self.lock:
            if op["status"] != "waiting":
                return self._view(op)
            if time.time() >= op["expires_at"]:
                op.update(status="expired", detail="Login expired. Start again.")
            elif "server" not in op and time.time() >= op["next_poll"]:
                op["next_poll"] = time.time() + op["interval"]
                try:
                    if op["product"] == "chatgpt":
                        try:
                            data = request_json(
                                self.providers,
                                "POST",
                                ISSUER + "/api/accounts/deviceauth/token",
                                json={
                                    "device_auth_id": op["device_auth_id"],
                                    "user_code": op["user_code"],
                                },
                            )
                        except AuthenticationRequiredError as exc:
                            if "HTTP 403" in str(exc) or "HTTP 404" in str(exc):
                                return self._view(op)
                            raise
                        tokens = request_json(
                            self.providers,
                            "POST",
                            ISSUER + "/oauth/token",
                            data={
                                "grant_type": "authorization_code",
                                "code": data["authorization_code"],
                                "redirect_uri": ISSUER + "/deviceauth/callback",
                                "client_id": OPENAI_CLIENT,
                                "code_verifier": data["code_verifier"],
                            },
                        )
                    else:
                        tokens = request_json(
                            self.providers,
                            "POST",
                            "https://github.com/login/oauth/access_token",
                            json={
                                "client_id": GITHUB_CLIENT,
                                "device_code": op["device_code"],
                                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                            },
                        )
                        if tokens.get("error") == "authorization_pending":
                            return self._view(op)
                        if tokens.get("error") == "slow_down":
                            op["interval"] += 5
                            op["next_poll"] = time.time() + op["interval"]
                            return self._view(op)
                    self._complete(op, tokens)
                except Exception:
                    op.update(status="error", detail="Login failed or was denied. Start again.")
            return self._view(op)

    def cancel_provider(self, provider_id):
        with self.lock:
            for op in self.operations.values():
                if op["provider_id"] == provider_id and op["status"] == "waiting":
                    op.update(status="cancelled", authorization_url=None, user_code=None)

    def cancel(self, ref):
        provider = self.providers.get(ref)
        self.cancel_provider(provider.id)
        return {**self.get(ref), "status": "cancelled"}

    def logout(self, ref):
        provider = self.providers.get(ref)
        self.cancel_provider(provider.id)
        with file_lock(self.providers._ctx.home / f"oauth-{provider.id}.lock"):
            self.providers.logout(ref)
        return self.get(ref)

    def close(self):
        for op in list(self.operations.values()):
            self.cancel_provider(op["provider_id"])
