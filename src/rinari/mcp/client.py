"""MCP client: initialize handshake + method wrappers (phase 5).

Thin, synchronous wrapper over a transport. The service owns one client per
connected server; the agent loop never talks JSON-RPC directly.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .protocol import (
    METHOD_INITIALIZE,
    METHOD_INITIALIZED,
    METHOD_PROMPTS_GET,
    METHOD_PROMPTS_LIST,
    METHOD_RESOURCES_LIST,
    METHOD_RESOURCES_READ,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    McpMessage,
    initialize_params,
    notification,
    request,
)
from .transport import McpTransport, TransportError

#: Server notification that its tool list changed (MCP `tools.listChanged`).
TOOLS_LIST_CHANGED = "notifications/tools/list_changed"
#: Pages read from one paginated list before giving up (a broken cursor loop).
MAX_LIST_PAGES = 50


class McpError(Exception):
    """Structured MCP failure (code + message)."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        *,
        http_status: int | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        # MCP_NOT_CONNECTED | MCP_PROTOCOL | MCP_TIMEOUT | MCP_DEPENDENCY
        # | MCP_TOOL_FAILED | MCP_NOT_FOUND | MCP_SPAWN_FAILED
        # | MCP_AUTH_REJECTED | MCP_UNREACHABLE | MCP_TLS | MCP_HTTP_ERROR
        # | MCP_TRANSPORT_UNSUPPORTED | MCP_SESSION_EXPIRED | MCP_SECRET_MISSING
        self.code = code
        self.message = message
        self.retryable = retryable
        # Remote transports: the HTTP status behind the failure, when any.
        self.http_status = http_status
        # Stable machine-readable key for the client UI (see docs/commands.md).
        self.hint = hint

    @staticmethod
    def from_transport(exc: TransportError) -> McpError:
        return McpError(
            _transport_code(exc),
            exc.message,
            retryable=exc.code in _RETRYABLE,
            http_status=exc.http_status,
            hint=exc.hint or _DEFAULT_HINTS.get(exc.code),
        )


@dataclass(frozen=True, slots=True)
class McpToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]
    output_schema: dict[str, Any] | None = None

    @staticmethod
    def from_raw(raw: dict[str, Any]) -> McpToolInfo:
        schema = raw.get("inputSchema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        annotations = raw.get("annotations") or {}
        if not isinstance(annotations, dict):
            annotations = {}
        output_schema = raw.get("outputSchema")
        if not isinstance(output_schema, dict):
            output_schema = None
        return McpToolInfo(
            name=str(raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            input_schema=schema,
            annotations=annotations,
            output_schema=output_schema,
        )


class McpClient:
    def __init__(self, transport: McpTransport, timeout_s: float = 30.0) -> None:
        self._transport = transport
        self._timeout_s = timeout_s
        self._started = False
        self._server_info: dict[str, Any] | None = None
        self._capabilities: dict[str, Any] = {}
        self._protocol_version: str | None = None
        self._instructions: str | None = None
        # Who hears the notifications the server sends on its own (C2:
        # `notifications/tools/list_changed` refreshes that server's tools).
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []
        transport.on_notification = self._on_notification

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> dict[str, Any] | None:
        if self._started:
            return self._server_info
        try:
            self._transport.start()
        except TransportError as exc:
            raise McpError.from_transport(exc) from exc
        version = getattr(self._transport, "preferred_protocol_version", None)
        try:
            frame = request(METHOD_INITIALIZE, initialize_params(version))
            message = self._transport.send(frame, self._timeout_s)
        except TransportError as exc:
            self._transport.close()
            raise McpError.from_transport(exc) from exc
        if message.has_error:
            self._transport.close()
            error = message.error if isinstance(message.error, dict) else {}
            raise McpError(
                "MCP_PROTOCOL",
                f"initialize failed: {error.get('message', message.error)}",
                hint="initialize_rejected",
            )
        result = message.result or {}
        if isinstance(result, dict):
            info = result.get("serverInfo")
            self._server_info = info if isinstance(info, dict) else None
            capabilities = result.get("capabilities")
            self._capabilities = capabilities if isinstance(capabilities, dict) else {}
            negotiated = result.get("protocolVersion")
            self._protocol_version = negotiated if isinstance(negotiated, str) else None
            # How to use its tools, in the server's words (MCP `initialize`):
            # shown with its group of tools (plan 0.4.0 v2, V2-6).
            note = result.get("instructions")
            self._instructions = note if isinstance(note, str) and note.strip() else None
        self._transport.notify(notification(METHOD_INITIALIZED))
        self._started = True
        return self._server_info

    def close(self) -> None:
        if not self._started:
            return
        self._started = False
        self._transport.close()

    @property
    def connected(self) -> bool:
        return self._started

    @property
    def server_info(self) -> dict[str, Any] | None:
        return self._server_info

    @property
    def capabilities(self) -> dict[str, Any]:
        """Server capabilities from `initialize` (tools/resources/prompts…)."""
        return self._capabilities

    @property
    def protocol_version(self) -> str | None:
        return self._protocol_version

    @property
    def instructions(self) -> str | None:
        return self._instructions

    def add_listener(self, listener: Callable[[str, dict[str, Any]], None]) -> None:
        """`listener(method, params)` for each server notification.

        It runs on the transport's reader thread: it must hand slow work
        (another request to this server) to its own thread.
        """
        self._listeners.append(listener)

    def _on_notification(self, message: McpMessage) -> None:
        method = message.method
        if not method:
            return
        for listener in list(self._listeners):
            with contextlib.suppress(Exception):
                listener(method, message.params)

    # -- methods ---------------------------------------------------------------

    def _call(self, method: str, params: dict[str, Any] | None) -> Any:
        if not self._started:
            raise McpError("MCP_NOT_CONNECTED", "client is not connected")
        try:
            frame = request(method, params)
            message = self._transport.send(frame, self._timeout_s)
        except TransportError as exc:
            if exc.code in ("TRANSPORT_SESSION_EXPIRED", "TRANSPORT_CLOSED", "TRANSPORT_IO"):
                # The session or the process is gone: the next use reconnects
                # (the service drops clients that are no longer connected).
                self.close()
            raise McpError.from_transport(exc) from exc
        if message.has_error:
            error = message.error or {}
            if int(error.get("code") or 0) == -32601:
                raise McpError("MCP_NOT_FOUND", f"unknown method: {method}")
            raise McpError("MCP_PROTOCOL", f"{method} failed: {error.get('message', error)}")
        return message.result

    def _list_all(self, method: str, key: str) -> list[Any]:
        """Every page of a paginated list (`nextCursor`), in order."""
        items: list[Any] = []
        cursor: str | None = None
        for _page in range(MAX_LIST_PAGES):
            params = {"cursor": cursor} if cursor else None
            result = self._call(method, params)
            items.extend(_as_list(result, key))
            following = result.get("nextCursor") if isinstance(result, dict) else None
            if not isinstance(following, str) or not following or following == cursor:
                break
            cursor = following
        return items

    def list_tools(self) -> list[McpToolInfo]:
        tools = self._list_all(METHOD_TOOLS_LIST, "tools")
        return [McpToolInfo.from_raw(t) for t in tools if isinstance(t, dict) and t.get("name")]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        # A progress token lets the server send notifications/progress while
        # it works; each one renews the wait (plan 0.4.0 #48), so a long job
        # that reports progress is not cut at the server timeout.
        params = {
            "name": name,
            "arguments": arguments or {},
            "_meta": {"progressToken": f"rinari-{uuid.uuid4().hex[:12]}"},
        }
        result = self._call(METHOD_TOOLS_CALL, params)
        if not isinstance(result, dict):
            return {"content": [], "raw": result}
        if result.get("isError"):
            text = _content_text(result.get("content"))
            raise McpError("MCP_TOOL_FAILED", text or "MCP tool returned an error")
        return result

    def list_resources(self) -> list[dict[str, Any]]:
        resources = self._list_all(METHOD_RESOURCES_LIST, "resources")
        return [r for r in resources if isinstance(r, dict)]

    def read_resource(self, uri: str) -> dict[str, Any]:
        result = self._call(METHOD_RESOURCES_READ, {"uri": uri})
        return result if isinstance(result, dict) else {"raw": result}

    def list_prompts(self) -> list[dict[str, Any]]:
        result = self._call(METHOD_PROMPTS_LIST, None)
        return [p for p in _as_list(result, "prompts") if isinstance(p, dict)]

    def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self._call(METHOD_PROMPTS_GET, {"name": name, "arguments": arguments or {}})
        return result if isinstance(result, dict) else {"raw": result}


_TRANSPORT_CODES = {
    "TRANSPORT_TIMEOUT": "MCP_TIMEOUT",
    "TRANSPORT_START": "MCP_SPAWN_FAILED",
    "TRANSPORT_AUTH": "MCP_AUTH_REJECTED",
    "TRANSPORT_UNREACHABLE": "MCP_UNREACHABLE",
    "TRANSPORT_TLS": "MCP_TLS",
    "TRANSPORT_NOT_FOUND": "MCP_NOT_FOUND",
    "TRANSPORT_SESSION_EXPIRED": "MCP_SESSION_EXPIRED",
    "TRANSPORT_HTTP": "MCP_HTTP_ERROR",
    "TRANSPORT_PROTOCOL": "MCP_PROTOCOL",
    "TRANSPORT_UNSUPPORTED": "MCP_TRANSPORT_UNSUPPORTED",
}
_RETRYABLE = frozenset({"TRANSPORT_TIMEOUT", "TRANSPORT_UNREACHABLE", "TRANSPORT_SESSION_EXPIRED"})
_DEFAULT_HINTS = {
    "TRANSPORT_START": "check_command_installed",
    "TRANSPORT_TIMEOUT": "server_slow_or_unresponsive",
    "TRANSPORT_IO": "server_exited",
    "TRANSPORT_CLOSED": "server_exited",
}


def _transport_code(exc: TransportError) -> str:
    return _TRANSPORT_CODES.get(exc.code, "MCP_DEPENDENCY")


def _as_list(result: Any, key: str) -> list[Any]:
    if isinstance(result, dict):
        value = result.get(key)
        return value if isinstance(value, list) else []
    if isinstance(result, list):
        return result
    return []


def _content_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


__all__ = ["TOOLS_LIST_CHANGED", "McpClient", "McpError", "McpToolInfo"]
