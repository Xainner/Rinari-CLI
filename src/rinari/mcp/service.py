"""McpService: server registry + connection cache + tool normalization.

Application-level facade over the `mcp_servers` table and the McpClient.
Connections are lazy (first use) and cached per process; `disconnect` drops
them. Project-scope servers require project trust before any connection.

Transports: `stdio` (subprocess) and `http` (Streamable HTTP, remote).
Configuration and secret handling live in `config.py`: literal secrets
(bearer tokens, secret headers, stdio env values) go to the CredentialStore
under `mcp/<server id>/<slot>`; the row keeps only references, so no secret
ever lands in the state DB (AGENTS.md 10), a view or a log line.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rinari.application.context import AppContext
from rinari.application.credentials import CredentialStore
from rinari.shared.clock import now_iso
from rinari.shared.errors import RinariError
from rinari.trust import TrustService

from .adapter import mcp_tool_definitions
from .client import McpClient, McpError, McpToolInfo
from .config import (
    AUTH_BEARER,
    DEFAULT_TIMEOUT_S,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
    TRANSPORTS,
    McpConfigError,
    SecretSlot,
    ServerDraft,
    apply_patch,
    config_from_draft,
    config_refs,
    draft_from_row,
    draft_view,
    is_owned_ref,
    slot_key,
    stored_config,
    validate_draft,
)
from .http_transport import StreamableHttpTransport
from .transport import InProcessTransport, McpTransport, StdioTransport

# Kept for compatibility with callers of the original (misspelled) name.
TRANSPORT_STDLIO = TRANSPORT_STDIO
#: Upper bound for one `test`/`probe` exchange, whatever the server timeout.
TEST_TIMEOUT_CAP_S = 30.0


def _now(ctx: AppContext) -> str:
    return now_iso(ctx.clock)


MAX_LOG_ENTRIES = 100

#: Hint keys by failure code when the transport gave none (docs/commands.md).
_HINTS = {
    "MCP_AUTH_REJECTED": "check_credentials",
    "MCP_UNREACHABLE": "check_url_or_network",
    "MCP_TLS": "check_tls_certificate",
    "MCP_TIMEOUT": "server_slow_or_unresponsive",
    "MCP_NOT_FOUND": "check_endpoint_path",
    "MCP_SPAWN_FAILED": "check_command_installed",
    "MCP_PROTOCOL": "not_an_mcp_endpoint",
    "MCP_HTTP_ERROR": "server_error",
    "MCP_TRANSPORT_UNSUPPORTED": "legacy_sse_unsupported",
    "MCP_SECRET_MISSING": "secret_missing",
    "MCP_SESSION_EXPIRED": "session_expired",
    "MCP_DEPENDENCY": "server_exited",
}


class McpService:
    def __init__(
        self,
        ctx: AppContext,
        trust: TrustService,
        credentials: CredentialStore | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._ctx = ctx
        self._trust = trust
        self._credentials = credentials or CredentialStore(ctx.layout)
        self._env: Mapping[str, str] = env if env is not None else os.environ
        self._clients: dict[str, McpClient] = {}
        self._logs: list[dict] = []

    def _log_entry(self, name: str, level: str, message: str) -> None:
        # Messages come from McpError/TransportError texts, which never carry
        # header values, tokens or query strings (http_transport.safe_url).
        self._logs.append(
            {"at": _now(self._ctx), "server": name, "level": level, "message": message}
        )
        if len(self._logs) > MAX_LOG_ENTRIES:
            del self._logs[: len(self._logs) - MAX_LOG_ENTRIES]

    # -- registry -------------------------------------------------------------

    def add(
        self,
        name: str,
        command: list[str] | None = None,
        *,
        scope: str = "global",
        env_refs: dict[str, str] | None = None,
        transport: str | None = None,
        url: str | None = None,
        env: dict[str, Any] | None = None,
        auth: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> dict:
        """Create (or replace) a server. Raises ValueError on invalid config."""
        if not isinstance(name, str) or not name.strip():
            raise McpConfigError("MCP servers need a name")
        if transport is not None and transport not in TRANSPORTS:
            raise McpConfigError(f"unsupported MCP transport: {transport!r} (stdio or http)")
        if transport is None:
            transport = TRANSPORT_HTTP if url and not command else TRANSPORT_STDIO
        params: dict[str, Any] = {
            "transport": transport,
            "command": list(command or []),
            "url": url,
            "env_refs": env_refs or None,
            "env": env,
            "auth": auth,
            "headers": headers,
            "timeout_s": timeout_s,
        }
        draft = apply_patch(ServerDraft(transport=transport), params)
        validate_draft(draft)
        existing = self._ctx.mcp_server_repo.find(name, scope)
        return self._write(name, scope, draft, existing, enabled=True)

    def update(self, name: str, params: Mapping[str, Any], *, scope: str = "global") -> dict:
        """Edit an existing server: transport, command/url, env, auth, headers.

        Maps merge per key (`null` removes one); a new literal secret replaces
        the stored one; switching auth to `none` clears the token. Raises
        LookupError for an unknown server, ValueError for invalid config.
        """
        existing = self._ctx.mcp_server_repo.find(name, scope)
        if existing is None:
            raise LookupError(name)
        draft = apply_patch(draft_from_row(existing), params)
        validate_draft(draft)
        return self._write(name, scope, draft, existing, enabled=bool(existing.get("enabled")))

    def _write(
        self,
        name: str,
        scope: str,
        draft: ServerDraft,
        existing: dict | None,
        *,
        enabled: bool,
    ) -> dict:
        server_id = existing["id"] if existing is not None else self._ctx.ids.new("mcp")
        created: list[str] = []
        try:
            self._store_literals(server_id, draft, created)
            config = config_from_draft(draft)
            row = self._ctx.mcp_server_repo.add(
                server_id,
                name=name,
                transport=draft.transport,
                command=" ".join(draft.argv) if draft.transport == TRANSPORT_STDIO else "",
                url=draft.url if draft.transport == TRANSPORT_HTTP else "",
                scope=scope,
                enabled=enabled,
                config_json=json.dumps(config, sort_keys=True),
                created_at=_now(self._ctx),
            )
        except Exception:
            # A secret stored for a write that never landed is not referenced.
            previous = config_refs(stored_config(existing)) if existing is not None else set()
            for ref in created:
                if ref not in previous:
                    with contextlib.suppress(Exception):
                        self._credentials.delete(ref)
            raise
        if existing is not None:
            self._retire_refs(config_refs(stored_config(existing)) - config_refs(config))
        self.disconnect(name)  # the next use connects with the new config
        return row

    def _store_literals(self, server_id: str, draft: ServerDraft, created: list[str]) -> None:
        def store(slot: SecretSlot | None, label: str) -> None:
            if slot is None or slot.literal is None:
                return
            try:
                ref = self._credentials.store_secret(slot_key(server_id, label), slot.literal)
            except RinariError as exc:
                raise McpConfigError(f"cannot store the secret for {label}: {exc.message}") from exc
            created.append(ref)
            slot.ref, slot.literal = ref, None

        if draft.auth_kind == AUTH_BEARER:
            store(draft.auth_token, "auth-token")
        for header_name, header in draft.headers.items():
            store(header.secret, f"header-{header_name.lower()}")
        for env_name, slot in draft.env.items():
            store(slot, f"env-{env_name}")

    def _retire_refs(self, refs: set[str]) -> None:
        for ref in refs:
            if is_owned_ref(ref):  # env:// references belong to the user
                with contextlib.suppress(Exception):
                    self._credentials.delete(ref)

    def remove(self, name: str, scope: str = "global") -> bool:
        self.disconnect(name)
        row = self._ctx.mcp_server_repo.find(name, scope)
        removed = self._ctx.mcp_server_repo.delete(name, scope)
        if removed and row is not None:
            self._retire_refs(config_refs(stored_config(row)))
        return removed

    def enable(self, name: str, scope: str = "global") -> dict | None:
        ok = self._ctx.mcp_server_repo.set_enabled(name, scope, True, _now(self._ctx))
        return self._ctx.mcp_server_repo.find(name, scope) if ok else None

    def disable(self, name: str, scope: str = "global") -> dict | None:
        ok = self._ctx.mcp_server_repo.set_enabled(name, scope, False, _now(self._ctx))
        row = self._ctx.mcp_server_repo.find(name, scope)
        if ok and row is not None:
            self.disconnect(name)
        return row if ok else None

    def list(self, scope: str | None = None) -> list[dict]:
        return self._ctx.mcp_server_repo.list(scope)

    def show(self, name: str, scope: str = "global") -> dict | None:
        return self._ctx.mcp_server_repo.find(name, scope)

    def view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Presentation-safe configuration of a row (no secret values)."""
        return draft_view(draft_from_row(row), self._env)

    # -- connections -------------------------------------------------------------

    def _trusted(self, row: dict, project: Path | None) -> bool:
        if row.get("scope") != "project":
            return True
        return project is not None and self._trust.is_trusted(project)

    def _resolve(self, slot: SecretSlot | None, label: str) -> str:
        if slot is None:
            raise McpError(
                "MCP_SECRET_MISSING", f"no secret configured for {label}", hint="secret_missing"
            )
        if slot.literal is not None:
            return slot.literal
        try:
            return self._credentials.resolve(slot.ref or "")
        except RinariError as exc:
            # The reason (env var unset, entry missing…) without any value.
            raise McpError(
                "MCP_SECRET_MISSING",
                f"secret for {label} is not available: {exc.message}",
                hint="secret_missing",
            ) from exc

    def _transport_for(self, row: dict) -> McpTransport:
        return self._transport_from_draft(draft_from_row(row))

    def _transport_from_draft(self, draft: ServerDraft) -> McpTransport:
        timeout = draft.timeout_s or DEFAULT_TIMEOUT_S
        if draft.transport == TRANSPORT_STDIO:
            env: dict[str, str] = {}
            for key, slot in draft.env.items():
                if slot.is_env:
                    # Legacy semantics: an unset process variable is skipped
                    # (servers often treat their env vars as optional).
                    value = self._env.get((slot.ref or "")[len("env://") :])
                    if value is not None:
                        env[key] = value
                else:
                    env[key] = self._resolve(slot, f"env.{key}")
            return StdioTransport(list(draft.argv), env=env or None)
        if draft.transport == TRANSPORT_HTTP:
            headers: dict[str, str] = {}
            for header_name, header in draft.headers.items():
                if header.secret is not None:
                    headers[header_name] = self._resolve(header.secret, f"headers.{header_name}")
                else:
                    headers[header_name] = header.value or ""
            if draft.auth_kind == AUTH_BEARER:
                token = self._resolve(draft.auth_token, "auth.token")
                headers["Authorization"] = f"Bearer {token}"
            return StreamableHttpTransport(draft.url, headers=headers, timeout_s=timeout)
        raise McpError(
            "MCP_TRANSPORT_UNSUPPORTED",
            f"unsupported transport: {draft.transport}",
            hint="legacy_sse_unsupported",
        )

    def _find_row(self, name: str) -> dict | None:
        for scope in ("global", "project"):
            row = self._ctx.mcp_server_repo.find(name, scope)
            if row is not None:
                return row
        return None

    def _client(self, name: str, project: Path | None = None) -> McpClient:
        cached = self._clients.get(name)
        if cached is not None and cached.connected:
            return cached
        row = self._find_row(name)
        if row is None:
            raise McpError(
                "MCP_NOT_FOUND", f"unknown MCP server: {name}", hint="server_not_registered"
            )
        if not row.get("enabled"):
            raise McpError(
                "MCP_NOT_CONNECTED", f"server {name} is disabled", hint="server_disabled"
            )
        if not self._trusted(row, project):
            raise McpError(
                "MCP_NOT_CONNECTED",
                f"server {name} is project-scoped and the project is not trusted",
                hint="project_not_trusted",
            )
        if cached is not None:
            cached.close()
        try:
            transport = self._transport_for(row)
        except McpError as exc:
            self._log_entry(name, "error", f"connect failed: {exc.message}")
            raise
        timeout = draft_from_row(row).timeout_s or DEFAULT_TIMEOUT_S
        client = McpClient(transport, timeout_s=timeout)
        self._clients[name] = client
        try:
            client.connect()
        except McpError as exc:
            self._log_entry(name, "error", f"connect failed: {exc.message}")
            raise
        self._log_entry(name, "info", f"connected ({row['transport']})")
        return client

    def connect(self, name: str, project: Path | None = None) -> dict | None:
        client = self._client(name, project)
        info = client.server_info or {}
        return {"name": name, "connected": True, "serverInfo": info}

    def client(self, name: str, project: Path | None = None) -> McpClient:
        """Public accessor for a connected client (introspection commands)."""
        return self._client(name, project)

    def is_connected(self, name: str) -> bool:
        client = self._clients.get(name)
        return bool(client is not None and client.connected)

    def disconnect(self, name: str) -> bool:
        client = self._clients.pop(name, None)
        if client is None:
            return False
        client.close()
        return True

    # -- capabilities -------------------------------------------------------------

    def tools(self, name: str, project: Path | None = None) -> list:
        client = self._client(name, project)
        infos: list[McpToolInfo] = client.list_tools()
        return mcp_tool_definitions(name, infos)

    def invoke(
        self,
        name: str,
        tool: str,
        arguments: dict,
        project: Path | None = None,
    ) -> dict:
        client = self._client(name, project)
        result = client.call_tool(tool, arguments)
        payload: dict = {"content": _content_summary(result.get("content"))}
        # Structured output is any JSON value the negotiated version allows;
        # never restrict it artificially to dict/list (§7).
        if "structuredContent" in result and result["structuredContent"] is not None:
            payload["structuredContent"] = result["structuredContent"]
        return payload

    def logs(self, name: str | None = None, limit: int = 50) -> list[dict]:
        entries = self._logs
        if name is not None:
            entries = [e for e in entries if e["server"] == name]
        return list(reversed(entries))[-limit:]

    # -- testing -----------------------------------------------------------------

    def test(self, name: str, project: Path | None = None) -> dict:
        """Diagnose a saved server with a fresh connection (never raises).

        A fresh client measures what the user changed, not a cached session;
        the connection turns use is left alone. A disabled server can still
        be tested (that is how a user checks it before enabling it).
        """
        row = self._find_row(name)
        if row is None:
            return _failure(
                name,
                None,
                McpError(
                    "MCP_NOT_FOUND", f"unknown MCP server: {name}", hint="server_not_registered"
                ),
            )
        if not self._trusted(row, project):
            return _failure(
                name,
                row.get("transport"),
                McpError(
                    "MCP_NOT_CONNECTED",
                    f"server {name} is project-scoped and the project is not trusted",
                    hint="project_not_trusted",
                ),
            )
        draft = draft_from_row(row)
        try:
            transport = self._transport_from_draft(draft)
        except McpError as exc:
            return _failure(name, draft.transport, exc)
        timeout = min(draft.timeout_s or DEFAULT_TIMEOUT_S, TEST_TIMEOUT_CAP_S)
        result = _diagnose(name, draft.transport, transport, timeout)
        if result["ok"]:
            self._log_entry(name, "info", "test ok")
        else:
            self._log_entry(name, "error", f"test failed: {result['message']}")
        return result

    def probe(
        self,
        params: Mapping[str, Any],
        *,
        name: str | None = None,
        scope: str = "global",
    ) -> dict:
        """Test a configuration without saving it (never stores a secret).

        With `name`, `params` is a patch over that saved server (the same
        merge as `update`), so an edit form can test a new URL without
        re-typing a stored token. Raises ValueError/LookupError on invalid
        input; connection failures are reported in the result.
        """
        if name is not None:
            existing = self._ctx.mcp_server_repo.find(name, scope)
            if existing is None:
                raise LookupError(name)
            draft = draft_from_row(existing)
        else:
            draft = ServerDraft()
        draft = apply_patch(draft, params)
        validate_draft(draft)
        try:
            transport = self._transport_from_draft(draft)
        except McpError as exc:
            return _failure(name, draft.transport, exc)
        timeout = min(draft.timeout_s or DEFAULT_TIMEOUT_S, TEST_TIMEOUT_CAP_S)
        return _diagnose(name, draft.transport, transport, timeout)


def _failure(
    name: str | None, transport: str | None, exc: McpError, started: float | None = None
) -> dict:
    return {
        "ok": False,
        "server": name,
        "transport": transport,
        "latency_ms": int((time.monotonic() - started) * 1000) if started is not None else None,
        "code": exc.code,
        "error": exc.code,  # legacy key (CLI, earlier clients)
        "message": exc.message,
        "http_status": exc.http_status,
        "hint": exc.hint or _HINTS.get(exc.code),
        "retryable": exc.retryable,
        "tools": 0,
    }


def _diagnose(
    name: str | None, transport_name: str | None, transport: McpTransport, timeout_s: float
) -> dict:
    started = time.monotonic()
    client = McpClient(transport, timeout_s=timeout_s)
    try:
        client.connect()
        tools = client.list_tools()
        latency = int((time.monotonic() - started) * 1000)
        capabilities = client.capabilities
        resources = _optional_count(client, "resources", capabilities)
        prompts = _optional_count(client, "prompts", capabilities)
    except McpError as exc:
        return _failure(name, transport_name, exc, started)
    finally:
        with contextlib.suppress(Exception):
            client.close()
        with contextlib.suppress(Exception):
            transport.close()
    info = client.server_info or {}
    return {
        "ok": True,
        "server": name,
        "transport": transport_name,
        "latency_ms": latency,
        "server_info": {
            "name": info.get("name") if isinstance(info.get("name"), str) else None,
            "version": info.get("version") if isinstance(info.get("version"), str) else None,
        },
        "serverInfo": info,  # legacy key
        "protocol_version": client.protocol_version,
        "tools": len(tools),
        "names": [t.name for t in tools],
        "resources": resources,
        "prompts": prompts,
    }


def _optional_count(client: McpClient, kind: str, capabilities: dict) -> int | None:
    """Count resources/prompts when the server advertises them; None otherwise."""
    if kind not in capabilities:
        return None
    try:
        items = client.list_resources() if kind == "resources" else client.list_prompts()
    except McpError:
        return None
    return len(items)


def _content_summary(content) -> list[dict]:
    if not isinstance(content, list):
        return []
    summary: list[dict] = []
    for item in content:
        if (
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ):
            summary.append({"type": "text", "text": item["text"]})
        elif isinstance(item, dict) and item.get("type") == "resource":
            summary.append({"type": "resource", "uri": item.get("uri")})
    return summary


__all__ = [
    "TRANSPORTS",
    "TRANSPORT_HTTP",
    "TRANSPORT_STDIO",
    "TRANSPORT_STDLIO",
    "InProcessTransport",
    "McpService",
]
