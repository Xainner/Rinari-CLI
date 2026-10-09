"""MCP server configuration: transports, authorization and secret slots.

A server row keeps the transport in its own column (`stdio` | `http`), the
URL in `url` and everything else in `config_json`:

```text
{
  "argv": ["npx", "-y", "server"],          stdio: exact command (args keep spaces)
  "env": {"API_KEY": "<ref>"},              stdio: environment for the process
  "auth": {"kind": "bearer", "token_ref": "<ref>"},   http: none | bearer | headers
  "headers": {"X-Team": {"value": "core"},  http: plain header
              "X-Api-Key": {"ref": "<ref>"}},           secret header
  "timeout_s": 30
}
```

Every `<ref>` is a secret reference (`env://VAR`, `keyring://mcp/<id>/…`,
`file://mcp/<id>/…`): values never reach the database, the config file, the
protocol views or the logs. A literal secret from a client is stored through
the CredentialStore under a slot owned by the server
(`mcp/<server id>/<slot>`); an `env://NAME` reference is kept as is and read
from the process environment at connection time. Clients can only name
environment variables: a `keyring://` or `file://` reference from outside is
rejected so a server can never be pointed at another stored secret (a
provider's API key, for instance).

Legacy rows (`{"env": {...}}` + space-joined `command`) keep working.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"
TRANSPORTS = (TRANSPORT_STDIO, TRANSPORT_HTTP)

AUTH_NONE = "none"
AUTH_BEARER = "bearer"
AUTH_HEADERS = "headers"
AUTH_KINDS = (AUTH_NONE, AUTH_BEARER, AUTH_HEADERS)

ENV_SCHEME = "env://"
#: Prefixes of the secret references this module owns (and may delete).
OWNED_REF_PREFIXES = ("keyring://mcp/", "file://mcp/")

MIN_TIMEOUT_S = 1.0
MAX_TIMEOUT_S = 300.0
DEFAULT_TIMEOUT_S = 30.0

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# RFC 7230 token characters.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}$")
_MAX_VALUE_CHARS = 8192
#: Headers the transport sets itself; a configured value would break the wire.
_RESERVED_HEADERS = frozenset(
    {
        "accept",
        "content-type",
        "content-length",
        "host",
        "connection",
        "transfer-encoding",
        "mcp-session-id",
        "mcp-protocol-version",
    }
)
#: Header names treated as secret unless the client says otherwise.
_SENSITIVE_HEADER_RE = re.compile(r"(auth|token|key|secret|password|cookie|session)", re.I)


class McpConfigError(ValueError):
    """Invalid MCP server configuration (message never carries a secret)."""


@dataclass(slots=True)
class SecretSlot:
    """A secret in a draft: an existing reference, or a literal to store."""

    ref: str | None = None
    literal: str | None = None

    @property
    def is_env(self) -> bool:
        return bool(self.ref and self.ref.startswith(ENV_SCHEME))


@dataclass(slots=True)
class HeaderSlot:
    value: str | None = None  # plain (non-secret) header
    secret: SecretSlot | None = None


@dataclass(slots=True)
class ServerDraft:
    transport: str = TRANSPORT_STDIO
    argv: list[str] = field(default_factory=list)
    url: str = ""
    env: dict[str, SecretSlot] = field(default_factory=dict)
    auth_kind: str = AUTH_NONE
    auth_token: SecretSlot | None = None
    headers: dict[str, HeaderSlot] = field(default_factory=dict)
    timeout_s: float | None = None

    def copy(self) -> ServerDraft:
        return copy.deepcopy(self)


# -- parsing stored rows --------------------------------------------------------


def stored_config(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        config = json.loads(row.get("config_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        config = {}
    return config if isinstance(config, dict) else {}


def draft_from_row(row: Mapping[str, Any]) -> ServerDraft:
    config = stored_config(row)
    transport = str(row.get("transport") or TRANSPORT_STDIO)
    argv = config.get("argv")
    if not (isinstance(argv, list) and all(isinstance(a, str) for a in argv)):
        argv = [p for p in str(row.get("command") or "").split() if p]
    draft = ServerDraft(transport=transport, argv=list(argv), url=str(row.get("url") or ""))
    env = config.get("env")
    if isinstance(env, dict):
        for key, ref in env.items():
            if isinstance(key, str) and isinstance(ref, str):
                draft.env[key] = SecretSlot(ref=ref)
    auth = config.get("auth")
    if isinstance(auth, dict):
        kind = auth.get("kind")
        if kind in AUTH_KINDS:
            draft.auth_kind = kind
        token_ref = auth.get("token_ref")
        if isinstance(token_ref, str) and token_ref:
            draft.auth_token = SecretSlot(ref=token_ref)
    headers = config.get("headers")
    if isinstance(headers, dict):
        for name, spec in headers.items():
            if not isinstance(name, str) or not isinstance(spec, dict):
                continue
            if isinstance(spec.get("ref"), str):
                draft.headers[name] = HeaderSlot(secret=SecretSlot(ref=spec["ref"]))
            elif isinstance(spec.get("value"), str):
                draft.headers[name] = HeaderSlot(value=spec["value"])
    timeout = config.get("timeout_s")
    if isinstance(timeout, int | float) and not isinstance(timeout, bool):
        draft.timeout_s = float(timeout)
    return draft


def config_refs(config: Mapping[str, Any]) -> set[str]:
    """Every secret reference a stored config points at."""
    refs: set[str] = set()
    env = config.get("env")
    if isinstance(env, dict):
        refs.update(v for v in env.values() if isinstance(v, str))
    auth = config.get("auth")
    if isinstance(auth, dict) and isinstance(auth.get("token_ref"), str):
        refs.add(auth["token_ref"])
    headers = config.get("headers")
    if isinstance(headers, dict):
        for spec in headers.values():
            if isinstance(spec, dict) and isinstance(spec.get("ref"), str):
                refs.add(spec["ref"])
    return refs


def is_owned_ref(ref: str) -> bool:
    return ref.startswith(OWNED_REF_PREFIXES)


# -- applying client input --------------------------------------------------------


def _env_name(name: Any, what: str) -> str:
    if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
        # Never echo the value: a pasted secret often lands in this field.
        raise McpConfigError(
            f"{what} must name an environment variable (letters, digits, '_'; "
            "not starting with a digit)"
        )
    return name


def parse_secret_input(value: Any, what: str) -> SecretSlot | None:
    """Client secret input → slot; None means "clear".

    Accepted: `"env://NAME"`, `{"env": "NAME"}`, `{"value": "literal"}` or a
    plain literal string. `keyring://`/`file://` references are refused.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        if set(value) == {"env"}:
            return SecretSlot(ref=ENV_SCHEME + _env_name(value["env"], what))
        if set(value) == {"value"}:
            value = value["value"]
        else:
            raise McpConfigError(f"{what} must be a string, {{'env': NAME}} or {{'value': ...}}")
    if not isinstance(value, str) or not value:
        raise McpConfigError(f"{what} must be a non-empty string")
    if value.startswith(ENV_SCHEME):
        return SecretSlot(ref=ENV_SCHEME + _env_name(value[len(ENV_SCHEME) :], what))
    if value.startswith(("keyring://", "file://")):
        raise McpConfigError(
            f"{what}: stored-secret references cannot be set by clients; "
            "pass the value or an env://NAME reference"
        )
    if len(value) > _MAX_VALUE_CHARS or "\n" in value or "\r" in value or "\0" in value:
        raise McpConfigError(f"{what} is too long or contains control characters")
    return SecretSlot(literal=value)


def validate_url(url: Any) -> str:
    if not isinstance(url, str) or not url.strip():
        raise McpConfigError("Param 'url' must be a non-empty http(s) URL")
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise McpConfigError("Param 'url' is not a valid URL") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise McpConfigError("Param 'url' must use http:// or https:// and name a host")
    if parts.username or parts.password:
        raise McpConfigError("Param 'url' must not embed credentials; use auth or headers instead")
    if len(url) > 2048:
        raise McpConfigError("Param 'url' is too long")
    return url


def is_local_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host in ("localhost",) or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _header_name(name: Any) -> str:
    if not isinstance(name, str) or not _HEADER_NAME_RE.match(name):
        raise McpConfigError("Header names must be HTTP tokens (letters, digits, '-', …)")
    if name.lower() in _RESERVED_HEADERS:
        raise McpConfigError(f"Header {name!r} is set by the transport and cannot be configured")
    return name


def _find_header(headers: dict[str, HeaderSlot], name: str) -> str | None:
    lowered = name.lower()
    for existing in headers:
        if existing.lower() == lowered:
            return existing
    return None


def apply_patch(draft: ServerDraft, params: Mapping[str, Any]) -> ServerDraft:
    """Apply client params (create/update/probe shape) to a draft copy.

    Maps (`env`, `headers`) merge per key; a `null` value removes the key.
    `auth` replaces the auth block; `{"kind": "bearer"}` without `token`
    keeps the stored token, `"token": null` is refused (use kind none).
    """
    draft = draft.copy()
    if "transport" in params and params["transport"] is not None:
        transport = params["transport"]
        if transport not in TRANSPORTS:
            raise McpConfigError("Param 'transport' must be 'stdio' or 'http'")
    elif params.get("url") and not params.get("command"):
        transport = TRANSPORT_HTTP
    elif params.get("command") and not params.get("url"):
        transport = TRANSPORT_STDIO
    else:
        transport = draft.transport
    if transport != draft.transport:
        # Switching transport drops what only the other one uses (and with it
        # the stored secrets, which the service retires after the write).
        if transport == TRANSPORT_HTTP:
            draft.argv, draft.env = [], {}
        else:
            draft.url, draft.auth_kind, draft.auth_token, draft.headers = "", AUTH_NONE, None, {}
        draft.transport = transport
    if "command" in params and params["command"] is not None:
        command = params["command"]
        if not isinstance(command, list) or not all(isinstance(c, str) for c in command):
            raise McpConfigError("Param 'command' must be a string array")
        draft.argv = [c for c in command if c != ""]
    if "url" in params:
        draft.url = validate_url(params["url"]) if params["url"] is not None else ""
    if "timeout_s" in params:
        timeout = params["timeout_s"]
        if timeout is None:
            draft.timeout_s = None
        elif (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not MIN_TIMEOUT_S <= float(timeout) <= MAX_TIMEOUT_S
        ):
            raise McpConfigError(
                f"Param 'timeout_s' must be between {MIN_TIMEOUT_S:g} and {MAX_TIMEOUT_S:g}"
            )
        else:
            draft.timeout_s = float(timeout)
    env_refs = params.get("env_refs")
    if env_refs is not None:
        # Legacy shape: references only, plain values rejected.
        if not isinstance(env_refs, dict):
            raise McpConfigError("Param 'env_refs' must be an object")
        for key, ref in env_refs.items():
            _env_name(key, "env_refs key")
            if not (isinstance(ref, str) and ref.startswith(ENV_SCHEME)):
                raise McpConfigError(
                    f"env ref for {key!r} must be 'env://PROCESS_VAR' "
                    "(plain values are never stored)"
                )
            draft.env[key] = SecretSlot(
                ref=ENV_SCHEME + _env_name(ref[len(ENV_SCHEME) :], f"env_refs.{key}")
            )
    env = params.get("env")
    if env is not None:
        if not isinstance(env, dict):
            raise McpConfigError("Param 'env' must be an object")
        for key, value in env.items():
            _env_name(key, "env key")
            slot = parse_secret_input(value, f"env.{key}")
            if slot is None:
                draft.env.pop(key, None)
            else:
                draft.env[key] = slot
    if "auth" in params and params["auth"] is not None:
        auth = params["auth"]
        if not isinstance(auth, dict) or auth.get("kind") not in AUTH_KINDS:
            raise McpConfigError("Param 'auth.kind' must be none, bearer or headers")
        kind = auth["kind"]
        if kind == AUTH_BEARER:
            if "token" in auth:
                token = parse_secret_input(auth["token"], "auth.token")
                if token is None:
                    raise McpConfigError(
                        "auth.token cannot be null for bearer auth; set kind 'none' to clear it"
                    )
                draft.auth_token = token
            elif draft.auth_kind != AUTH_BEARER:
                draft.auth_token = None
        else:
            draft.auth_token = None
        draft.auth_kind = kind
    headers = params.get("headers")
    if headers is not None:
        if not isinstance(headers, dict):
            raise McpConfigError("Param 'headers' must be an object")
        for name, spec in headers.items():
            name = _header_name(name)
            existing = _find_header(draft.headers, name)
            if spec is None:
                if existing is not None:
                    draft.headers.pop(existing)
                continue
            if isinstance(spec, str):
                spec = {"value": spec}
            if not isinstance(spec, dict) or "value" not in spec:
                raise McpConfigError(
                    f"Header {name!r} must be a string or {{'value': ..., 'secret': bool}}"
                )
            secret = spec.get("secret")
            if secret is None:
                secret = bool(_SENSITIVE_HEADER_RE.search(name))
            if not isinstance(secret, bool):
                raise McpConfigError(f"Header {name!r}: 'secret' must be a boolean")
            if existing is not None:
                draft.headers.pop(existing)
            if secret:
                slot = parse_secret_input(spec["value"], f"headers.{name}")
                draft.headers[name] = HeaderSlot(secret=slot)
            else:
                value = spec["value"]
                if (
                    not isinstance(value, str)
                    or len(value) > _MAX_VALUE_CHARS
                    or any(c in value for c in "\r\n\0")
                ):
                    raise McpConfigError(f"Header {name!r} must be a single-line string")
                draft.headers[name] = HeaderSlot(value=value)
    return draft


def validate_draft(draft: ServerDraft) -> None:
    if draft.transport == TRANSPORT_STDIO:
        if not draft.argv:
            raise McpConfigError("stdio MCP servers need a command (e.g. ['npx', '-y', '...'])")
        if draft.auth_kind != AUTH_NONE or draft.headers:
            raise McpConfigError("auth and headers apply to http servers only")
    else:
        if not draft.url:
            raise McpConfigError("http MCP servers need a 'url'")
        if draft.env:
            raise McpConfigError("env applies to stdio servers only")
        if draft.auth_kind == AUTH_BEARER and draft.auth_token is None:
            raise McpConfigError("bearer auth needs a token (value or env://NAME)")
        if draft.auth_kind == AUTH_BEARER and _find_header(draft.headers, "Authorization"):
            raise McpConfigError("bearer auth and an Authorization header cannot be combined")


def slot_key(server_id: str, slot: str) -> str:
    """Credential-store key of one secret slot of a server."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", slot)
    return f"mcp/{server_id}/{safe}"


def config_from_draft(draft: ServerDraft) -> dict[str, Any]:
    """Serialize a draft whose secrets are all references (after storing)."""

    def ref(slot: SecretSlot) -> str:
        if slot.ref is None:  # pragma: no cover - guarded by the service
            raise McpConfigError("internal: secret not stored")
        return slot.ref

    config: dict[str, Any] = {}
    if draft.transport == TRANSPORT_STDIO:
        config["argv"] = list(draft.argv)
        if draft.env:
            config["env"] = {k: ref(v) for k, v in sorted(draft.env.items())}
    else:
        auth: dict[str, Any] = {"kind": draft.auth_kind}
        if draft.auth_kind == AUTH_BEARER and draft.auth_token is not None:
            auth["token_ref"] = ref(draft.auth_token)
        config["auth"] = auth
        if draft.headers:
            headers: dict[str, Any] = {}
            for name, header in sorted(draft.headers.items()):
                if header.secret is not None:
                    headers[name] = {"ref": ref(header.secret)}
                else:
                    headers[name] = {"value": header.value or ""}
            config["headers"] = headers
    if draft.timeout_s is not None:
        config["timeout_s"] = draft.timeout_s
    return config


# -- presentation (never secrets) ---------------------------------------------------


def _secret_view(slot: SecretSlot | None, env: Mapping[str, str]) -> dict[str, Any]:
    if slot is None:
        return {"configured": False, "source": None}
    if slot.is_env:
        name = (slot.ref or "")[len(ENV_SCHEME) :]
        return {"configured": bool(env.get(name)), "source": "env", "env_var": name}
    return {"configured": True, "source": "stored"}


def draft_view(draft: ServerDraft, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Presentation-safe view: flags and env-var names, never values."""
    env = os.environ if env is None else env
    warnings: list[str] = []
    if (
        draft.transport == TRANSPORT_HTTP
        and draft.url.startswith("http://")
        and not is_local_host(draft.url)
    ):
        # Credentials over plain HTTP to another machine travel readable.
        warnings.append("plain_http_remote")
    view: dict[str, Any] = {
        "url": draft.url if draft.transport == TRANSPORT_HTTP else "",
        "argv": list(draft.argv) if draft.transport == TRANSPORT_STDIO else [],
        "timeout_s": draft.timeout_s,
        "auth": {"kind": draft.auth_kind},
        "headers": [],
        "env": [],
        "warnings": warnings,
    }
    if draft.auth_kind == AUTH_BEARER:
        view["auth"]["token"] = _secret_view(draft.auth_token, env)
    for name, header in sorted(draft.headers.items(), key=lambda kv: kv[0].lower()):
        if header.secret is not None:
            view["headers"].append(
                {"name": name, "secret": True, **_secret_view(header.secret, env)}
            )
        else:
            view["headers"].append(
                {"name": name, "secret": False, "configured": True, "value": header.value}
            )
    for name, slot in sorted(draft.env.items()):
        view["env"].append({"name": name, **_secret_view(slot, env)})
    return view


__all__ = [
    "AUTH_BEARER",
    "AUTH_HEADERS",
    "AUTH_KINDS",
    "AUTH_NONE",
    "DEFAULT_TIMEOUT_S",
    "TRANSPORTS",
    "TRANSPORT_HTTP",
    "TRANSPORT_STDIO",
    "HeaderSlot",
    "McpConfigError",
    "SecretSlot",
    "ServerDraft",
    "apply_patch",
    "config_from_draft",
    "config_refs",
    "draft_from_row",
    "draft_view",
    "is_local_host",
    "is_owned_ref",
    "parse_secret_input",
    "slot_key",
    "stored_config",
    "validate_draft",
    "validate_url",
]
