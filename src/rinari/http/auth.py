"""Auth injection and secret redaction for http.* tools (phase 5).

Secrets only ever enter the request through resolved *secret references*
(`env://VAR`, `file://key`) via the session CredentialStore — tool arguments
never carry plaintext secrets. Responses and final URLs are redacted before
they can reach the model or traces.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from rinari.web.client import WebRequestError

SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "auth-token",
        "cookie",
        "set-cookie",
        "token",
    }
)

_HEADER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
_QUERY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


def redact_headers(headers: Mapping[str, object]) -> dict[str, str]:
    """Copy of `headers` with every sensitive value replaced by `***`."""
    out: dict[str, str] = {}
    for key, value in headers.items():
        out[str(key)] = "***" if str(key).lower() in SENSITIVE_HEADER_NAMES else str(value)
    return out


def redact_url(url: str, secret_query_names: Iterable[str] = ()) -> str:
    """Return `url` with the *values* of known secret query params removed."""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    secret = {name.lower() for name in secret_query_names}
    pairs = []
    for pair in parsed.query.split("&"):
        key, sep, _ = pair.partition("=")
        pairs.append(key if sep and key.lower() in secret else pair)
    return urlunparse(parsed._replace(query="&".join(pairs)))


@dataclass(frozen=True, slots=True)
class AuthInjection:
    headers: dict[str, str]
    query: dict[str, str]
    secret_query_names: tuple[str, ...]


def apply_auth(auth: object, credentials: object) -> AuthInjection:
    """Build (headers, query) for an auth spec.

    Auth spec shapes (all require `ref`, a secret reference):

    - {"type": "bearer", "ref": "env://API_TOKEN"}
    - {"type": "basic", "username": "u", "ref": "env://API_TOKEN"}
    - {"type": "header", "name": "X-Api-Key", "ref": "env://API_TOKEN"}
    - {"type": "query", "name": "api_key", "ref": "env://API_TOKEN"}
    """
    if auth is None:
        return AuthInjection({}, {}, ())
    if not isinstance(auth, dict):
        raise WebRequestError("INVALID_ARGUMENT", "auth must be an object")
    auth_type = auth.get("type")
    if auth_type not in ("bearer", "basic", "header", "query"):
        raise WebRequestError(
            "INVALID_ARGUMENT",
            "auth.type must be one of: bearer, basic, header, query",
        )
    value = _resolve(auth.get("ref"), credentials)
    if auth_type == "bearer":
        return AuthInjection({"Authorization": f"Bearer {value}"}, {}, ())
    if auth_type == "basic":
        username = auth.get("username")
        if not isinstance(username, str) or not username:
            raise WebRequestError("INVALID_ARGUMENT", "basic auth requires username")
        encoded = base64.b64encode(f"{username}:{value}".encode()).decode("ascii")
        return AuthInjection({"Authorization": f"Basic {encoded}"}, {}, ())
    name = auth.get("name")
    if not isinstance(name, str):
        raise WebRequestError("INVALID_ARGUMENT", "auth.name must be a string")
    if auth_type == "header":
        if not _HEADER_NAME_RE.match(name):
            raise WebRequestError("INVALID_ARGUMENT", f"invalid header name: {name!r}")
        return AuthInjection({name: value}, {}, ())
    if not _QUERY_NAME_RE.match(name):
        raise WebRequestError("INVALID_ARGUMENT", f"invalid query param name: {name!r}")
    return AuthInjection({}, {name: value}, (name,))


def _resolve(ref: object, credentials: object) -> str:
    if not isinstance(ref, str) or not ref:
        raise WebRequestError(
            "INVALID_ARGUMENT",
            "auth.ref must be a secret reference (env://VAR or file://key)",
        )
    resolver = getattr(credentials, "resolve", None)
    if credentials is None or resolver is None:
        raise WebRequestError(
            "AUTH_REQUIRED",
            f"Cannot resolve secret reference {ref!r}: session has no credential store",
        )
    try:
        value: str = resolver(ref)
    except Exception as exc:
        raise WebRequestError("AUTH_REQUIRED", f"Secret reference not resolvable: {exc}") from exc
    if not value:
        raise WebRequestError("AUTH_EXPIRED", "Secret reference resolved to an empty value")
    return value
