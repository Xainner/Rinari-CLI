"""Connector catalog (plan de conectores C4).

The catalog is data, not code per service: `catalog.json` ships with the
Engine and is validated against `catalog.schema.json` (in the tests and on
load). Each entry says what a server is (a remote URL with its
authorization, or a local command with the environment it needs) and what
adding it takes, following the C0 table:

- `registration`: dynamic client registration (RFC 7591); nothing to type.
- `client_metadata`: Rinari's client ID metadata document (CIMD), or the
  owner's own `client_id` as the alternative.
- `own_client`: the owner's own OAuth client (`client_id`, often
  `client_secret`, and a `redirect_port` when the app needs an exact redirect).
- `token`: a token sent as Bearer (GitHub), with an own OAuth app as the
  alternative.
- `secrets`: a local server's environment (Postgres, SQLite), or a
  connection string split into it.

`create_params` turns an entry plus what the owner typed into the
`McpService.add` arguments; secrets go through the usual slots (credential
store), never into the row. A saved server is matched back to its entry by
its URL (scheme, host and path) or its command, so «ya agregada» needs no
extra column and also covers a server added by hand or imported.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .config import (
    AUTH_BEARER,
    AUTH_OAUTH,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
    McpConfigError,
    draft_from_row,
)

CATALOG_PATH = Path(__file__).with_name("catalog.json")
SCHEMA_PATH = Path(__file__).with_name("catalog.schema.json")

SETUP_REGISTRATION = "registration"
SETUP_CLIENT_METADATA = "client_metadata"
SETUP_OWN_CLIENT = "own_client"
SETUP_TOKEN = "token"
SETUP_SECRETS = "secrets"


class CatalogError(ValueError):
    """The shipped catalog does not match its schema (a packaging bug)."""


def validate_catalog(document: Any, schema: Mapping[str, Any] | None = None) -> list[str]:
    """Every problem of a catalog document: schema violations, then consistency."""
    from rinari.tools.schema import validate_against

    if schema is None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = validate_against(dict(schema), document)
    if errors:
        return errors
    seen: set[str] = set()
    urls: set[str] = set()
    for entry in document["entries"]:
        where = f"$.entries[{entry['id']}]"
        if entry["id"] in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(entry["id"])
        server, setup = entry["server"], entry["setup"]
        if server["transport"] == TRANSPORT_HTTP:
            key = _url_key(server["url"])
            if key in urls:
                errors.append(f"{where}: another entry has the same URL")
            urls.add(key)
            wanted = AUTH_BEARER if setup["kind"] == SETUP_TOKEN else AUTH_OAUTH
            if server["auth"]["kind"] != wanted:
                errors.append(f"{where}: setup {setup['kind']} needs auth kind {wanted}")
            if setup["kind"] == SETUP_SECRETS:
                errors.append(f"{where}: setup secrets is for local servers")
        else:
            if setup["kind"] != SETUP_SECRETS:
                errors.append(f"{where}: local servers use setup secrets")
            names = {item["name"] for item in server.get("env", [])}
            mapped = set(server.get("connection_string", {}).get("env", {}).values())
            if not mapped <= names:
                errors.append(f"{where}: connection_string maps to unknown env names")
        fields = {field["name"] for field in setup.get("fields", [])}
        if setup["kind"] == SETUP_OWN_CLIENT and "client_id" not in fields:
            errors.append(f"{where}: own_client needs a client_id field")
        if setup["kind"] == SETUP_TOKEN and "token" not in fields:
            errors.append(f"{where}: token setup needs a token field")
        for alternative in setup.get("alternatives", []):
            alt_fields = {field["name"] for field in alternative.get("fields", [])}
            if alternative["kind"] == SETUP_OWN_CLIENT and "client_id" not in alt_fields:
                errors.append(f"{where}: own_client alternative needs a client_id field")
    return errors


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    document = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    errors = validate_catalog(document)
    if errors:
        raise CatalogError("; ".join(errors[:5]))
    return document


def catalog() -> dict[str, Any]:
    """The catalog document (`version`, `verified_at`, `entries`); a deep copy."""
    return json.loads(json.dumps(_load()))


def entries() -> list[dict[str, Any]]:
    return catalog()["entries"]


def entry(entry_id: str) -> dict[str, Any] | None:
    for item in entries():
        if item["id"] == entry_id:
            return item
    return None


# -- matching saved servers -------------------------------------------------------


def _url_key(url: str) -> str:
    """scheme://host[:port]/path without query, fragment or trailing slash."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{parts.scheme.lower()}://{host}{parts.path.rstrip('/')}"


def entry_for_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The catalog entry a saved server corresponds to, if any."""
    draft = draft_from_row(row)
    for item in entries():
        server = item["server"]
        if server["transport"] != draft.transport:
            continue
        if draft.transport == TRANSPORT_HTTP and _url_key(server["url"]) == _url_key(draft.url):
            return item
        if draft.transport == TRANSPORT_STDIO and list(server["command"]) == list(draft.argv):
            return item
    return None


def catalog_description(row: Mapping[str, Any], lang: str = "en") -> str | None:
    """What a saved server is for, from its catalog entry (C6's hook).

    One sentence in `lang` (`es` or `en`, English otherwise), or None when
    the server is not a catalog connector. Owner-independent data shipped
    with the Engine, never the server's own claims.
    """
    item = entry_for_row(row)
    if item is None:
        return None
    description = item["description"]
    return description.get(lang) or description["en"]


def catalog_description_for(mcp: Any, name: str, lang: str = "en") -> str | None:
    """`catalog_description` of the saved server called `name` (any scope)."""
    row = mcp.show(name) or mcp.show(name, "project")
    return catalog_description(row, lang) if row is not None else None


def catalog_lookup(mcp: Any, lang: str = "en") -> Callable[[Mapping[str, Any]], str | None]:
    """C6's `catalog` hook for `McpService.connectors()` rows.

    Those rows carry the server's name and state, not its saved config, so
    the entry is found through the saved server of that name.
    """
    return lambda row: catalog_description_for(mcp, str(row.get("name") or ""), lang)


def list_view(rows: list[Mapping[str, Any]], status: Any = None) -> list[dict[str, Any]]:
    """Every entry plus `added` and the saved servers that match it.

    `status(row)` (optional) gives each server's `{status, connection}`.
    """
    matches: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = entry_for_row(row)
        if item is None:
            continue
        server: dict[str, Any] = {
            "name": row.get("name"),
            "scope": row.get("scope") or "global",
            "enabled": bool(row.get("enabled")),
        }
        if status is not None:
            server["status"] = status(row).get("status")
        matches.setdefault(item["id"], []).append(server)
    out = []
    for item in entries():
        servers = matches.get(item["id"], [])
        out.append({**item, "added": bool(servers), "servers": servers})
    return out


# -- turning an entry into mcp.create ------------------------------------------------


def _need(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise McpConfigError(f"{what} is required for this connector")
    return value.strip()


def _setup_for(item: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    """The setup the inputs follow: the entry's own or one of its alternatives."""
    setup = item["setup"]
    if setup["kind"] == SETUP_TOKEN and not inputs.get("token") and inputs.get("client_id"):
        for alternative in setup.get("alternatives", []):
            if alternative["kind"] == SETUP_OWN_CLIENT:
                return alternative
    if setup["kind"] == SETUP_CLIENT_METADATA and inputs.get("client_id"):
        for alternative in setup.get("alternatives", []):
            if alternative["kind"] == SETUP_OWN_CLIENT:
                return alternative
    return setup


def _required_fields(setup: Mapping[str, Any]) -> list[str]:
    return [field["name"] for field in setup.get("fields", []) if field.get("required")]


def _parse_connection_string(spec: Mapping[str, Any], value: str) -> dict[str, str]:
    """A connection string split into the entry's env names (never echoed)."""
    mapping = spec["env"]
    has_scheme = "://" in value and value.split("://", 1)[0].isalpha()
    if "path" in mapping:
        if has_scheme:
            parts = urlsplit(value)
            if parts.scheme.lower() not in spec["schemes"]:
                raise McpConfigError("connection_string: unsupported scheme")
            path = unquote(parts.netloc + parts.path) if parts.netloc else unquote(parts.path)
            if parts.scheme.lower() == "sqlite" and path.startswith("/"):
                # SQLAlchemy's form: sqlite:///relative.db, sqlite:////absolute.db
                path = path[1:]
            elif len(path) > 2 and path[0] == "/" and path[2] == ":":
                path = path[1:]  # file:///C:/data.db → C:/data.db
        else:
            path = value
        if not path:
            raise McpConfigError("connection_string: the database path is empty")
        return {mapping["path"]: path}
    if not has_scheme:
        raise McpConfigError("connection_string must be a URL like scheme://user@host/database")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise McpConfigError("connection_string is not a valid URL") from None
    if parts.scheme.lower() not in spec["schemes"]:
        raise McpConfigError("connection_string: unsupported scheme")
    values = {
        "host": unquote(parts.hostname or ""),
        "port": str(port) if port else "",
        "database": unquote(parts.path.lstrip("/")),
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "query": parts.query,
    }
    return {mapping[key]: text for key, text in values.items() if key in mapping and text}


def create_params(item: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    """`McpService.add` keyword arguments for `item` with the owner's inputs.

    Inputs: `client_id`, `client_secret`, `redirect_port`, `token`, `secrets`
    ({ENV_NAME: value}, local servers) and `connection_string`. Raises
    McpConfigError (whose message never carries a value) when something the
    entry needs is missing or does not belong to it.
    """
    server = item["server"]
    if server["transport"] == TRANSPORT_STDIO:
        return _local_params(item, inputs)
    setup = _setup_for(item, inputs)
    for name in _required_fields(setup):
        value = inputs.get(name)
        if value is None or (isinstance(value, str | dict) and not value):
            raise McpConfigError(f"{name} is required for this connector")
    unexpected = {"secrets", "connection_string"} & {k for k, v in inputs.items() if v}
    if unexpected:
        raise McpConfigError(f"{sorted(unexpected)[0]} applies to local connectors only")
    if setup["kind"] == SETUP_TOKEN:
        return {
            "transport": TRANSPORT_HTTP,
            "url": server["url"],
            # A literal, "env://NAME" or {env: NAME}: config.parse_secret_input decides.
            "auth": {"kind": AUTH_BEARER, "token": inputs["token"]},
        }
    if inputs.get("token"):
        raise McpConfigError("token: this connector signs in with OAuth")
    auth: dict[str, Any] = {"kind": AUTH_OAUTH}
    template = server["auth"]
    if template.get("scopes"):
        auth["scopes"] = list(template["scopes"])
    if template.get("authorize_params"):
        auth["authorize_params"] = dict(template["authorize_params"])
    if inputs.get("client_id"):
        auth["client_id"] = _need(inputs["client_id"], "client_id")
    if inputs.get("client_secret"):
        auth["client_secret"] = inputs["client_secret"]
    if inputs.get("redirect_port") is not None:
        auth["redirect_port"] = inputs["redirect_port"]
    return {"transport": TRANSPORT_HTTP, "url": server["url"], "auth": auth}


def _local_params(item: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    server = item["server"]
    for key in ("client_id", "client_secret", "token", "redirect_port"):
        if inputs.get(key) not in (None, ""):
            raise McpConfigError(f"{key} applies to remote connectors only")
    specs = {spec["name"]: spec for spec in server.get("env", [])}
    given = inputs.get("secrets") or {}
    if not isinstance(given, Mapping):
        raise McpConfigError("secrets must be an object of environment values")
    unknown = sorted(set(given) - set(specs))
    if unknown:
        raise McpConfigError(f"secrets.{unknown[0]} is not used by this connector")
    env: dict[str, str] = {}
    connection = inputs.get("connection_string")
    if connection:
        if "connection_string" not in server:
            raise McpConfigError("connection_string is not used by this connector")
        if not isinstance(connection, str):
            raise McpConfigError("connection_string must be a string")
        env.update(_parse_connection_string(server["connection_string"], connection.strip()))
    for name, value in given.items():
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise McpConfigError(f"secrets.{name} must be a string")
        env[name] = value
    for name, spec in specs.items():
        if name not in env and spec.get("default"):
            env[name] = spec["default"]
    missing = [name for name, spec in specs.items() if spec["required"] and name not in env]
    if missing:
        raise McpConfigError(f"this connector needs {', '.join(missing)}")
    return {"transport": TRANSPORT_STDIO, "command": list(server["command"]), "env": env or None}


def next_step(item: Mapping[str, Any], params: Mapping[str, Any]) -> str:
    """What the client does after adding: `sign_in` (mcp.auth.start) or `connect`."""
    auth = params.get("auth") or {}
    return "sign_in" if auth.get("kind") == AUTH_OAUTH else "connect"


__all__ = [
    "CATALOG_PATH",
    "SCHEMA_PATH",
    "CatalogError",
    "catalog",
    "catalog_description",
    "catalog_description_for",
    "create_params",
    "entries",
    "entry",
    "entry_for_row",
    "list_view",
    "next_step",
    "validate_catalog",
]
