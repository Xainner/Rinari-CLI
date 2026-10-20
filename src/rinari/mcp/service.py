"""McpService: server registry + connection cache + tool normalization.

Application-level facade over the `mcp_servers` table and the McpClient.
Connections live in the process (the Engine's, shared by every session) and
are made in the background, in parallel, without ever making a turn wait
(plan de conectores, C2); `disconnect` drops one. Each server has a state
(`connecting`, `connected`, `reauth_required`, `error` with its hint,
`disabled`, or `idle` before anything was tried) and an event when it
changes. Project-scope servers require project trust before any connection.

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
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rinari.application.context import AppContext
from rinari.application.credentials import CredentialStore
from rinari.shared.clock import now_iso
from rinari.shared.errors import RinariError
from rinari.trust import TrustService

from . import oauth
from . import permissions as tool_permissions
from .adapter import mcp_tool_definitions, with_permissions
from .client import TOOLS_LIST_CHANGED, McpClient, McpError, McpToolInfo
from .config import (
    AUTH_BEARER,
    AUTH_OAUTH,
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
from .resources import resource_tools
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

#: Connection states of a server (C2). `idle`: enabled and not tried yet (a
#: project server waiting for its trusted project, or the CLI before first use).
STATUS_IDLE = "idle"
STATUS_CONNECTING = "connecting"
STATUS_CONNECTED = "connected"
STATUS_REAUTH = "reauth_required"
STATUS_ERROR = "error"
STATUS_DISABLED = "disabled"
STATUSES = (
    STATUS_IDLE,
    STATUS_CONNECTING,
    STATUS_CONNECTED,
    STATUS_REAUTH,
    STATUS_ERROR,
    STATUS_DISABLED,
)
#: The renewal of a sign-in failed (C1): only the owner can fix it.
_REAUTH_HINTS = frozenset({"reauth_required"})
#: Failures waiting cannot fix: the owner has to change something first.
_OWNER_ACTION_HINTS = frozenset(
    {
        "oauth_required",
        "check_credentials",
        "secret_missing",
        "check_command_installed",
        "check_endpoint_path",
        "check_tls_certificate",
        "not_an_mcp_endpoint",
        "legacy_sse_unsupported",
        "initialize_rejected",
    }
)
#: Growing wait between attempts at a failed server: 2, 4, 8… up to 5 min.
RETRY_BASE_S = 2.0
RETRY_MAX_S = 300.0
#: Timer retries before the Engine waits for a call or «Reconectar».
MAX_AUTO_RETRIES = 6
#: Margin over the server's timeouts when a call joins an attempt in flight.
_JOIN_MARGIN_S = 5.0


@dataclass
class _ServerState:
    status: str = STATUS_IDLE
    error: McpError | None = None
    changed_at: str | None = None
    # ToolDefinitions of the last `tools/list`; kept while it reconnects.
    tools: list | None = None
    resources: bool = False
    scope: str = "global"
    project: Path | None = None
    # Failed attempts in a row, and when the next one may run (monotonic).
    attempts: int = 0
    retry_at: float | None = None
    timer: threading.Timer | None = None
    # Set when the attempt in flight ends; None when none runs.
    attempt: threading.Event | None = None
    # Bumped by disconnect/reconfigure: an older attempt's result is dropped.
    generation: int = 0
    # What the server says it is for (`serverInfo`, `instructions`) at its
    # last connection; kept while it reconnects (C6: the model's index).
    about: dict[str, str] | None = None


class _Follower:
    """A registry that receives the live servers' tools (a turn's runtime)."""

    __slots__ = ("__weakref__", "project", "registry")

    def __init__(self, registry: Any, project: Path | None) -> None:
        self.registry = registry
        self.project = project


def _unavailable(name: str, state: _ServerState, now: float) -> McpError:
    """The stored failure, said without waiting for another timeout."""
    error = state.error
    wait = max(0, round((state.retry_at or now) - now))
    reason = error.message if error is not None else "not connected"
    return McpError(
        error.code if error is not None else "MCP_NOT_CONNECTED",
        f"MCP server {name} is unavailable ({reason}); it is retried in {wait}s "
        "or when the owner reconnects it",
        retryable=True,
        http_status=error.http_status if error is not None else None,
        hint=(error.hint or _HINTS.get(error.code)) if error is not None else None,
    )


def _plain(exc: McpError, name: str) -> McpError:
    """A failed sign-in renewal said so the model tells the owner (C6)."""
    if (exc.hint or "") not in _REAUTH_HINTS:
        return exc
    return McpError(
        exc.code,
        f"MCP server {name}: {exc.message.rstrip('.')}. Its sign-in has to be renewed in "
        "Settings → Connectors (Reconnect); tell the owner instead of trying another way.",
        http_status=exc.http_status,
        hint=exc.hint,
    )


def _about(client: McpClient) -> dict[str, str]:
    """`serverInfo.description`/`title` and `instructions`, as the server sent them."""
    info = client.server_info or {}
    about = {
        key: info[key]
        for key in ("description", "title")
        if isinstance(info.get(key), str) and info[key].strip()
    }
    note = getattr(client, "instructions", None)
    if isinstance(note, str) and note.strip():
        about["instructions"] = note
    return about


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
        # Connection state (C2). One lock for states, clients and followers;
        # no I/O runs while it is held.
        self._lock = threading.RLock()
        self._states: dict[str, _ServerState] = {}
        self._followers: weakref.WeakSet[_Follower] = weakref.WeakSet()
        self._outbox: list[str] = []
        # Timers retry failed servers only in a serving Engine (start_background).
        self._autoconnect = False
        self._monotonic: Callable[[], float] = time.monotonic
        #: `on_status(view)` when a server's state changes (the Engine's event).
        self.on_status: Callable[[dict[str, Any]], None] | None = None

    def _log_entry(self, name: str, level: str, message: str) -> None:
        # Messages come from McpError/TransportError texts, which never carry
        # header values, tokens or query strings (http_transport.safe_url).
        with self._lock:
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
            if existing is not None:
                # The owner's tool permissions (C5) survive an edit of the server.
                kept = stored_config(existing).get(tool_permissions.PERMISSIONS_KEY)
                if kept:
                    config[tool_permissions.PERMISSIONS_KEY] = kept
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
        self._restart(name)  # connects again with the new config
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
        if draft.auth_kind == AUTH_OAUTH and draft.oauth is not None:
            store(draft.oauth.client_secret, "oauth-client-secret")
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
            # OAuth tokens: revoked when possible, then deleted (before the
            # client secret they may need is retired).
            oauth.forget_server(
                self._credentials, row["id"], draft_from_row(row), home=self._ctx.home
            )
            self._retire_refs(config_refs(stored_config(row)))
        if removed and self._find_row(name) is None:
            with self._lock:
                self._states.pop(name, None)
        return removed

    def enable(self, name: str, scope: str = "global") -> dict | None:
        ok = self._ctx.mcp_server_repo.set_enabled(name, scope, True, _now(self._ctx))
        if ok:
            self._restart(name)
        return self._ctx.mcp_server_repo.find(name, scope) if ok else None

    def disable(self, name: str, scope: str = "global") -> dict | None:
        ok = self._ctx.mcp_server_repo.set_enabled(name, scope, False, _now(self._ctx))
        row = self._ctx.mcp_server_repo.find(name, scope)
        if ok and row is not None:
            self.disconnect(name)
            with self._lock:
                state = self._states.get(name)
                if state is not None:
                    self._set(name, state, STATUS_DISABLED, None)
            self._flush()
        return row if ok else None

    def list(self, scope: str | None = None) -> list[dict]:
        return self._ctx.mcp_server_repo.list(scope)

    def show(self, name: str, scope: str = "global") -> dict | None:
        return self._ctx.mcp_server_repo.find(name, scope)

    def view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Presentation-safe configuration and connection state (no secret values)."""
        return {**draft_view(draft_from_row(row), self._env), **self.status(row)}

    # -- connections (C2: in the Engine process, in parallel, never blocking) ---
    #
    # Each enabled server has a state: idle → connecting → connected, or
    # error / reauth_required / disabled. Connecting runs on its own daemon
    # thread, so neither «Preparando» nor a turn waits for any server. A
    # connected server keeps its client and its last `tools/list`, shared by
    # every session of this process; the server's `list_changed` refreshes it.
    # Registries that follow the service (one per turn runtime) receive a
    # server's tools when it connects or changes. A server that fails keeps
    # its state and hint; it is retried with a growing wait (on a timer in
    # the Engine, or when one of its tools is called) or by `reconnect`.

    def _visible(self, scope: str | None, project: Path | None) -> bool:
        if scope != "project":
            return True
        return project is not None and self._trust.is_trusted(project)

    def _trusted(self, row: dict, project: Path | None) -> bool:
        return self._visible(row.get("scope"), project)

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
        return self._transport_from_draft(draft_from_row(row), row.get("id"))

    def _transport_from_draft(
        self, draft: ServerDraft, server_id: str | None = None
    ) -> McpTransport:
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
            return StreamableHttpTransport(
                draft.url,
                headers=headers,
                timeout_s=timeout,
                home=self._ctx.home,
                # Saved OAuth servers only: tokens are kept per server id.
                auth=oauth.session_for(
                    self._credentials, server_id or "", draft, home=self._ctx.home
                ),
            )
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

    # -- state -------------------------------------------------------------

    def _state(self, name: str) -> _ServerState:
        state = self._states.get(name)
        if state is None:
            state = self._states[name] = _ServerState()
        return state

    def _set(self, name: str, state: _ServerState, status: str, error: McpError | None) -> None:
        """Change a server's status; the event goes out after the lock (`_flush`)."""
        state.status = status
        state.error = error
        state.changed_at = _now(self._ctx)
        self._outbox.append(name)

    def _flush(self) -> None:
        """Send the status events queued under the lock, outside of it."""
        with self._lock:
            names, self._outbox = self._outbox, []
            payloads = [
                {"name": name, **self._state_view(self._states[name])}
                for name in dict.fromkeys(names)
                if name in self._states
            ]
        listener = self.on_status
        if listener is None:
            return
        for payload in payloads:
            with contextlib.suppress(Exception):  # a client never breaks a server
                listener(payload)

    def _state_view(self, state: _ServerState) -> dict[str, Any]:
        error = state.error
        retry_in = None
        if state.retry_at is not None and state.status == STATUS_ERROR:
            retry_in = max(0, round(state.retry_at - self._monotonic()))
        return {
            "status": state.status,
            "connection": {
                "code": error.code if error is not None else None,
                "hint": (error.hint or _HINTS.get(error.code)) if error is not None else None,
                "message": error.message if error is not None else None,
                "tools": len(state.tools or ()),
                "resources": state.resources,
                "attempts": state.attempts,
                "retry_in_s": retry_in,
                "changed_at": state.changed_at,
            },
        }

    def status(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """`{status, connection}` of a saved server for views (never a secret)."""
        with self._lock:
            state = self._states.get(str(row.get("name")))
            if not row.get("enabled"):
                view = self._state_view(_ServerState(status=STATUS_DISABLED))
                view["connection"]["changed_at"] = state.changed_at if state else None
                return view
            return self._state_view(state if state is not None else _ServerState())

    # -- connecting ----------------------------------------------------------

    def _begin(self, name: str, project: Path | None) -> tuple[threading.Event, int] | None:
        """Claim the next attempt for `name` (under the lock); None if one runs."""
        state = self._state(name)
        if state.attempt is not None:
            return None
        if state.timer is not None:
            state.timer.cancel()
            state.timer = None
        if project is not None:
            state.project = project
        state.attempt = threading.Event()
        state.retry_at = None
        self._set(name, state, STATUS_CONNECTING, state.error)
        return state.attempt, state.generation

    def _spawn(self, name: str, project: Path | None) -> None:
        """Connect `name` on its own thread (caller holds the lock)."""
        claimed = self._begin(name, project)
        if claimed is None:
            return
        done, generation = claimed
        threading.Thread(
            target=self._attempt,
            args=(name, project, generation, done),
            name=f"mcp-connect-{name}",
            daemon=True,
        ).start()

    def _open(self, name: str, project: Path | None) -> tuple[McpClient, list, bool, str]:
        """Connect and list the tools (no lock held: this is the slow part)."""
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
        scope = row.get("scope") or "global"
        try:
            transport = self._transport_for(row)
        except McpError as exc:
            self._log_entry(name, "error", f"connect failed: {exc.message}")
            raise
        timeout = draft_from_row(row).timeout_s or DEFAULT_TIMEOUT_S
        client = McpClient(transport, timeout_s=timeout)
        try:
            client.connect()
            infos = client.list_tools()
        except McpError as exc:
            self._log_entry(name, "error", f"connect failed: {exc.message}")
            with contextlib.suppress(Exception):
                client.close()
            raise
        self._log_entry(name, "info", f"connected ({row['transport']})")
        resources = "resources" in (client.capabilities or {})
        return client, self._definitions(name, client, infos), resources, scope

    def _attempt(
        self, name: str, project: Path | None, generation: int, done: threading.Event
    ) -> McpError | None:
        """One connection attempt; returns why it failed, or None."""
        stale: McpClient | None = None
        failure: McpError | None = None
        try:
            try:
                client, definitions, resources, scope = self._open(name, project)
            except McpError as exc:
                failure = exc
            except Exception as exc:  # a connect thread never dies silently
                failure = McpError(
                    "MCP_DEPENDENCY",
                    f"connect failed: {type(exc).__name__}",
                    hint="server_error",
                )
            with self._lock:
                state = self._states.get(name)
                current = state is not None and state.generation == generation
                if current and state.attempt is done:
                    state.attempt = None
                if failure is not None:
                    if current:
                        self._fail(name, state, failure)
                elif not current:
                    stale = client  # disconnected or reconfigured meanwhile
                else:
                    stale = self._clients.get(name)
                    self._clients[name] = client
                    state.tools = definitions
                    state.resources = resources
                    state.scope = scope
                    state.about = _about(client)
                    state.attempts = 0
                    state.retry_at = None
                    self._set(name, state, STATUS_CONNECTED, None)
                    client.add_listener(
                        lambda method, _params: self._on_notice(name, generation, method)
                    )
                    self._sync(name)
        finally:
            done.set()
            if stale is not None:
                with contextlib.suppress(Exception):
                    stale.close()
            self._flush()
        return failure

    def _fail(self, name: str, state: _ServerState, exc: McpError) -> None:
        """Record a failed attempt and when to try again (under the lock)."""
        hint = exc.hint or _HINTS.get(exc.code)
        if hint == "server_not_registered":
            self._states.pop(name, None)
            self._sync(name)
            return
        if hint == "project_not_trusted":
            # Another, trusted project may still use it: nothing to keep.
            state.attempts = 0
            self._set(name, state, STATUS_IDLE, None)
            return
        if hint == "server_disabled":
            self._set(name, state, STATUS_DISABLED, None)
            return
        if hint in _REAUTH_HINTS:
            self._set(name, state, STATUS_REAUTH, exc)
            return
        state.attempts += 1
        self._set(name, state, STATUS_ERROR, exc)
        if hint in _OWNER_ACTION_HINTS:
            return
        delay = min(RETRY_BASE_S * 2 ** (state.attempts - 1), RETRY_MAX_S)
        state.retry_at = self._monotonic() + delay
        if self._autoconnect and state.attempts <= MAX_AUTO_RETRIES:
            timer = threading.Timer(delay, self._retry, (name, state.generation))
            timer.daemon = True
            state.timer = timer
            timer.start()

    def _retry(self, name: str, generation: int) -> None:
        with self._lock:
            state = self._states.get(name)
            if state is None or state.generation != generation or not self._autoconnect:
                return
            state.timer = None
            if state.status == STATUS_ERROR:
                self._spawn(name, state.project)
        self._flush()

    def _dropped(self, name: str, state: _ServerState, exc: McpError | None = None) -> None:
        """A connected server went away (under the lock): keep its tools, reconnect."""
        self._clients.pop(name, None)
        state.attempts = 0
        state.retry_at = None
        self._set(
            name,
            state,
            STATUS_ERROR,
            exc or McpError("MCP_DEPENDENCY", "the server connection closed", hint="server_exited"),
        )
        self._log_entry(name, "error", "connection lost")
        if self._autoconnect:
            self._spawn(name, state.project)

    def _client(self, name: str, project: Path | None = None, *, force: bool = False) -> McpClient:
        """The connected client of `name`, connecting it in this thread if needed.

        A call joins an attempt already in flight. Without `force` (a tool
        call), a server that failed is retried only once its wait is over,
        and one that needs a new sign-in is not retried at all: the call
        fails at once with the stored reason instead of waiting a timeout.
        """
        with self._lock:
            cached = self._clients.get(name)
            state = self._states.get(name)
            if cached is not None and state is not None:
                if not self._visible(state.scope, project):
                    raise McpError(
                        "MCP_NOT_CONNECTED",
                        f"server {name} is project-scoped and the project is not trusted",
                        hint="project_not_trusted",
                    )
                if cached.connected and state.status == STATUS_CONNECTED:
                    return cached
                if not cached.connected and state.status == STATUS_CONNECTED:
                    self._dropped(name, state)
            state = self._state(name)
            joining = state.attempt
            claimed = None
            if joining is None:
                if not force and state.status == STATUS_REAUTH and state.error is not None:
                    raise _plain(state.error, name)
                if (
                    not force
                    and state.status == STATUS_ERROR
                    and state.retry_at is not None
                    and self._monotonic() < state.retry_at
                ):
                    raise _unavailable(name, state, self._monotonic())
                claimed = self._begin(name, project)
        self._flush()
        failure: McpError | None = None
        if claimed is not None:
            failure = self._attempt(name, project, claimed[1], claimed[0])
        elif joining is not None:
            joining.wait(self._join_timeout(name))
        with self._lock:
            client = self._clients.get(name)
            state = self._states.get(name)
            if (
                client is not None
                and client.connected
                and state is not None
                and state.status == STATUS_CONNECTED
            ):
                return client
            if failure is None and state is not None:
                failure = state.error
        if failure is not None:
            raise _plain(failure, name)
        raise McpError("MCP_NOT_CONNECTED", f"server {name} is not connected", retryable=True)

    def _join_timeout(self, name: str) -> float:
        row = self._find_row(name)
        timeout = (draft_from_row(row).timeout_s if row is not None else None) or DEFAULT_TIMEOUT_S
        return timeout * 2 + _JOIN_MARGIN_S  # initialize + tools/list

    def start(self, project: Path | None = None) -> None:
        """Connect, in the background, every enabled server not tried yet.

        Never waits. Global servers, and project ones when `project` is
        trusted. Servers already connecting, connected or waiting for their
        retry are left alone. Called when a session builds its runtime.
        """
        rows = self.list()
        scopes: dict[str, set] = {}
        for row in rows:
            scopes.setdefault(row["name"], set()).add(row.get("scope"))
        with self._lock:
            for row in rows:
                name = row["name"]
                if row.get("scope") == "project" and "global" in scopes[name]:
                    continue  # the global server of the same name wins (`_find_row`)
                state = self._states.get(name)
                if not row.get("enabled"):
                    if state is not None and state.status != STATUS_DISABLED:
                        self._set(name, state, STATUS_DISABLED, None)
                    continue
                if not self._visible(row.get("scope"), project):
                    continue
                state = self._state(name)
                if state.status in (STATUS_IDLE, STATUS_DISABLED) and state.attempt is None:
                    state.scope = row.get("scope") or "global"
                    self._spawn(name, project)
        self._flush()

    def start_background(self) -> None:
        """The Engine connects its servers at start and retries them on timers."""
        self._autoconnect = True
        self.start(None)

    def stop_background(self) -> None:
        """Stop the retry timers (the connections stay)."""
        with self._lock:
            self._autoconnect = False
            for state in self._states.values():
                if state.timer is not None:
                    state.timer.cancel()
                    state.timer = None

    def reconnect(self, name: str, project: Path | None = None) -> dict[str, Any]:
        """«Reconectar»: a new connection now, in the background, whatever the wait.

        Returns the server's state (`connecting`, unless it is disabled).
        Raises LookupError for an unknown server.
        """
        row = self._find_row(name)
        if row is None:
            raise LookupError(name)
        previous: McpClient | None = None
        with self._lock:
            state = self._state(name)
            if not row.get("enabled"):
                self._set(name, state, STATUS_DISABLED, None)
            elif state.attempt is None:
                # A fresh session: the old one is dropped, whatever its state.
                previous = self._clients.pop(name, None)
                state.attempts = 0
                state.scope = row.get("scope") or "global"
                self._spawn(name, project or state.project)
            view = self._state_view(state)
        if previous is not None:
            with contextlib.suppress(Exception):
                previous.close()
        self._log_entry(name, "info", "reconnect requested")
        self._flush()
        return view

    def connect(self, name: str, project: Path | None = None) -> dict | None:
        client = self._client(name, project, force=True)
        info = client.server_info or {}
        return {"name": name, "connected": True, "serverInfo": info}

    def client(self, name: str, project: Path | None = None) -> McpClient:
        """Public accessor for a connected client (introspection commands)."""
        return self._client(name, project, force=True)

    def is_connected(self, name: str) -> bool:
        with self._lock:
            client = self._clients.get(name)
            state = self._states.get(name)
            return bool(
                client is not None
                and client.connected
                and state is not None
                and state.status == STATUS_CONNECTED
            )

    def disconnect(self, name: str) -> bool:
        """Drop the connection; the next use (or `start`) connects again."""
        with self._lock:
            client = self._clients.pop(name, None)
            state = self._states.get(name)
            if state is not None:
                state.generation += 1
                if state.timer is not None:
                    state.timer.cancel()
                    state.timer = None
                state.attempt = None
                state.tools = None
                state.resources = False
                state.attempts = 0
                state.retry_at = None
                self._set(name, state, STATUS_IDLE, None)
                self._sync(name)
        if client is not None:
            client.close()
        self._flush()
        return client is not None

    def _restart(self, name: str) -> None:
        """New configuration or enabled again: the Engine connects it at once."""
        self.disconnect(name)
        if self._autoconnect:
            row = self._find_row(name)
            with self._lock:
                if row is not None and row.get("enabled") and self._visible(row.get("scope"), None):
                    self._state(name).scope = row.get("scope") or "global"
                    self._spawn(name, None)
            self._flush()

    def close(self) -> None:
        """Stop the timers and close every connection (end of the process)."""
        self.stop_background()
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            with contextlib.suppress(Exception):
                client.close()

    # -- notifications -------------------------------------------------------

    def _on_notice(self, name: str, generation: int, method: str) -> None:
        """A server notification, on its transport's thread: never block here."""
        if method != TOOLS_LIST_CHANGED:
            return
        threading.Thread(
            target=self._refresh_tools,
            args=(name, generation),
            name=f"mcp-tools-{name}",
            daemon=True,
        ).start()

    def _refresh_tools(self, name: str, generation: int) -> None:
        with self._lock:
            state = self._states.get(name)
            client = self._clients.get(name)
            if state is None or client is None or state.generation != generation:
                return
        try:
            infos = client.list_tools()
        except McpError as exc:
            self._log_entry(name, "error", f"tools refresh failed: {exc.message}")
            with self._lock:
                if state.generation == generation and not client.connected:
                    self._dropped(name, state, exc)
            self._flush()
            return
        definitions = self._definitions(name, client, infos)
        with self._lock:
            if state.generation != generation:
                return
            state.tools = definitions
            self._outbox.append(name)
            self._sync(name)
        self._log_entry(name, "info", f"tools refreshed ({len(definitions)})")
        self._flush()

    # -- registries that follow the live servers --------------------------------

    def follow(self, registry: Any, project: Path | None = None) -> None:
        """Put the known tools in `registry` and keep it up to date.

        For a turn's registry: a server that connects later, changes its
        tools or goes away updates it while the turn runs (`capability.search`
        and `tool.call` read the registry on each call). The model's tool list
        never changes: MCP tools are always on demand (F42). The registry
        keeps the follower alive; the service only holds a weak reference.
        """
        follower = _Follower(registry, project)
        with self._lock:
            self._followers.add(follower)
            registry.followers.append(follower)
            for name in list(self._states):
                self._sync_one(follower, name)
            self._sync_resources(follower)

    def available_tools(self, project: Path | None = None) -> list:
        """The tools known right now for a session in `project` (no waiting)."""
        with self._lock:
            tools = [
                tool
                for state in self._states.values()
                if state.tools and self._visible(state.scope, project)
                for tool in state.tools
            ]
            if self._resource_servers_locked(project):
                tools.extend(resource_tools())
        return tools

    def _sync(self, name: str) -> None:
        for follower in list(self._followers):
            with contextlib.suppress(Exception):
                self._sync_one(follower, name)
                self._sync_resources(follower)

    def _sync_one(self, follower: _Follower, name: str) -> None:
        state = self._states.get(name)
        tools = (
            list(state.tools or ())
            if state is not None and self._visible(state.scope, follower.project)
            else []
        )
        follower.registry.replace_where(
            lambda tool: (
                (tool.manifest or {}).get("source") == "mcp"
                and (tool.manifest or {}).get("server") == name
            ),
            tools,
        )

    def _sync_resources(self, follower: _Follower) -> None:
        wanted = bool(self._resource_servers_locked(follower.project))
        follower.registry.replace_where(
            lambda tool: bool((tool.manifest or {}).get("mcp_resources")),
            resource_tools() if wanted else [],
        )

    def _resource_servers_locked(self, project: Path | None) -> list[str]:
        return sorted(
            name
            for name, state in self._states.items()
            if state.resources
            and state.status == STATUS_CONNECTED
            and self._visible(state.scope, project)
        )

    def connectors(self, project: Path | None = None) -> list[dict[str, Any]]:
        """The servers a session in `project` can see, with their state now (C6).

        `{name, status, hint}` plus what the server said it is for at its
        last connection (`description`, `title`, `instructions`), sorted by
        name and never a secret. For the model's index of what is connected.
        """
        with self._lock:
            return [
                {
                    "name": name,
                    "status": state.status,
                    "hint": (
                        (state.error.hint or _HINTS.get(state.error.code))
                        if state.error is not None
                        else None
                    ),
                    **(state.about or {}),
                }
                for name, state in sorted(self._states.items())
                if self._visible(state.scope, project)
            ]

    def connectors_for(self, registry: Any) -> list[dict[str, Any]] | None:
        """`connectors` for a registry that follows the service, else None.

        A registry that never followed (a remote target's runtime) gets no
        MCP server in its index: none of their tools can reach it.
        """
        with self._lock:
            follower = next(
                (
                    f
                    for f in getattr(registry, "followers", ())
                    if isinstance(f, _Follower) and f in self._followers
                ),
                None,
            )
        return self.connectors(follower.project) if follower is not None else None

    def resource_servers(self, project: Path | None = None) -> list[str]:
        """Connected servers that offer resources, for a session in `project`."""
        with self._lock:
            return self._resource_servers_locked(project)

    # -- capabilities -------------------------------------------------------------

    def _definitions(self, name: str, client: McpClient, infos: list[McpToolInfo]) -> list:
        definitions = mcp_tool_definitions(name, infos, self.permissions(name))
        note = getattr(client, "instructions", None)
        if note:
            from dataclasses import replace

            definitions = [
                replace(tool, manifest={**tool.manifest, "server_instructions": note})
                for tool in definitions
            ]
        return definitions

    # -- the owner's permissions per tool (connectors plan, C5) -------------------

    def permissions(self, name: str, scope: str | None = None) -> dict[str, Any]:
        """The server's `{default, tools}` settings (`mcp/permissions.py`)."""
        row = self._find_row(name) if scope is None else self._ctx.mcp_server_repo.find(name, scope)
        return tool_permissions.stored_permissions(stored_config(row) if row is not None else None)

    def known_tools(self, name: str) -> list:
        """The server's tools from its last `tools/list`, without connecting."""
        with self._lock:
            state = self._states.get(name)
            return list(state.tools or ()) if state is not None else []

    def set_tool_permission(
        self, name: str, tool: str, *, scope: str | None = None, **changes: Any
    ) -> dict[str, Any]:
        """Change one tool's `decision` and/or `annotations` (only those given).

        Raises LookupError for an unknown server, ValueError for bad input.
        The connection is kept: the tools are classified again in place.
        """
        row = self._permission_row(name, scope)
        updated = tool_permissions.with_tool_setting(
            tool_permissions.stored_permissions(stored_config(row)), tool, **changes
        )
        return self._save_permissions(row, updated)

    def set_default_permission(
        self, name: str, decision: str | None, *, scope: str | None = None
    ) -> dict[str, Any]:
        """The server's Allow / Ask / Block for tools without their own setting."""
        row = self._permission_row(name, scope)
        updated = tool_permissions.with_default(
            tool_permissions.stored_permissions(stored_config(row)), decision
        )
        return self._save_permissions(row, updated)

    def _permission_row(self, name: str, scope: str | None) -> dict:
        row = self._find_row(name) if scope is None else self._ctx.mcp_server_repo.find(name, scope)
        if row is None:
            raise LookupError(name)
        return row

    def _save_permissions(self, row: dict, updated: dict[str, Any]) -> dict[str, Any]:
        config = stored_config(row)
        block = tool_permissions.serialized(updated)
        if block is None:
            config.pop(tool_permissions.PERMISSIONS_KEY, None)
        else:
            config[tool_permissions.PERMISSIONS_KEY] = block
        # Same row, same id and secrets: only config_json changes.
        self._ctx.mcp_server_repo.add(
            row["id"],
            name=row["name"],
            transport=row.get("transport") or TRANSPORT_STDIO,
            command=row.get("command") or "",
            url=row.get("url") or "",
            scope=row.get("scope") or "global",
            enabled=bool(row.get("enabled")),
            config_json=json.dumps(config, sort_keys=True),
            created_at=_now(self._ctx),
        )
        saved = tool_permissions.stored_permissions(config)
        name = row["name"]
        live = self._find_row(name)  # the global row wins over a project one
        if live is not None and live.get("id") == row["id"]:
            with self._lock:
                state = self._states.get(name)
                if state is not None and state.tools:
                    state.tools = with_permissions(state.tools, saved)
                    self._sync(name)
        self._log_entry(name, "info", "tool permissions changed")
        return saved

    def tools(self, name: str, project: Path | None = None) -> list:
        """The server's tools, connecting it if needed; `tools/list` is cached."""
        self._client(name, project, force=True)
        with self._lock:
            state = self._states.get(name)
            return list(state.tools or ()) if state is not None else []

    def _failed_call(self, name: str, client: McpClient, exc: McpError) -> None:
        """What a failed call says about the connection (C1: `reauth_required`)."""
        with self._lock:
            state = self._states.get(name)
            if state is None or self._clients.get(name) is not client:
                return
            if (exc.hint or "") in _REAUTH_HINTS:
                self._clients.pop(name, None)
                self._set(name, state, STATUS_REAUTH, exc)
            elif not client.connected:
                self._dropped(name, state, exc)
        self._flush()

    def invoke(
        self,
        name: str,
        tool: str,
        arguments: dict,
        project: Path | None = None,
    ) -> dict:
        client = self._client(name, project)
        try:
            result = client.call_tool(tool, arguments)
        except McpError as exc:
            self._failed_call(name, client, exc)
            raise _plain(exc, name)  # noqa: B904 - the same error, or said plainly
        payload: dict = {"content": _content_summary(result.get("content"))}
        # Structured output is any JSON value the negotiated version allows;
        # never restrict it artificially to dict/list (§7).
        if "structuredContent" in result and result["structuredContent"] is not None:
            payload["structuredContent"] = result["structuredContent"]
        return payload

    def _resources_client(self, name: str, project: Path | None) -> McpClient:
        client = self._client(name, project)
        if "resources" not in (client.capabilities or {}):
            raise McpError(
                "MCP_NOT_FOUND", f"server {name} does not offer resources", hint="no_resources"
            )
        return client

    def list_resources(self, name: str, project: Path | None = None) -> list[dict]:
        client = self._resources_client(name, project)
        try:
            return client.list_resources()
        except McpError as exc:
            self._failed_call(name, client, exc)
            raise _plain(exc, name)  # noqa: B904 - the same error, or said plainly

    def read_resource(self, name: str, uri: str, project: Path | None = None) -> dict:
        client = self._resources_client(name, project)
        try:
            return client.read_resource(uri)
        except McpError as exc:
            self._failed_call(name, client, exc)
            raise _plain(exc, name)  # noqa: B904 - the same error, or said plainly

    def logs(self, name: str | None = None, limit: int = 50) -> list[dict]:
        with self._lock:
            entries = list(self._logs)
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
            transport = self._transport_from_draft(draft, row.get("id"))
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
        server_id: str | None = None
        if name is not None:
            existing = self._ctx.mcp_server_repo.find(name, scope)
            if existing is None:
                raise LookupError(name)
            draft = draft_from_row(existing)
            server_id = existing.get("id")
        else:
            draft = ServerDraft()
        draft = apply_patch(draft, params)
        validate_draft(draft)
        try:
            transport = self._transport_from_draft(draft, server_id)
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
    "STATUSES",
    "STATUS_CONNECTED",
    "STATUS_CONNECTING",
    "STATUS_DISABLED",
    "STATUS_ERROR",
    "STATUS_IDLE",
    "STATUS_REAUTH",
    "TRANSPORTS",
    "TRANSPORT_HTTP",
    "TRANSPORT_STDIO",
    "TRANSPORT_STDLIO",
    "InProcessTransport",
    "McpService",
]
