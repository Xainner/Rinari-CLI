"""MCP client: initialize handshake + method wrappers (phase 5).

Thin, synchronous wrapper over a transport. The service owns one client per
connected server; the agent loop never talks JSON-RPC directly.
"""

from __future__ import annotations

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
    initialize_params,
    notification,
    request,
)
from .transport import McpTransport, TransportError


class McpError(Exception):
    """Structured MCP failure (code + message)."""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        # MCP_NOT_CONNECTED | MCP_PROTOCOL | MCP_TIMEOUT | MCP_DEPENDENCY
        # | MCP_TOOL_FAILED | MCP_NOT_FOUND
        self.code = code
        self.message = message
        self.retryable = retryable


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

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> dict[str, Any] | None:
        if self._started:
            return self._server_info
        try:
            self._transport.start()
        except TransportError as exc:
            raise McpError(_transport_code(exc), exc.message) from exc
        try:
            frame = request(METHOD_INITIALIZE, initialize_params())
            message = self._transport.send(frame, self._timeout_s)
        except TransportError as exc:
            self._transport.close()
            raise McpError(
                _transport_code(exc), exc.message, retryable=exc.code == "TRANSPORT_TIMEOUT"
            ) from exc
        if message.has_error:
            self._transport.close()
            raise McpError(
                "MCP_PROTOCOL",
                f"initialize failed: {message.error.get('message', message.error)}",
            )
        result = message.result or {}
        if isinstance(result, dict):
            self._server_info = result.get("serverInfo")
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

    # -- methods ---------------------------------------------------------------

    def _call(self, method: str, params: dict[str, Any] | None) -> Any:
        if not self._started:
            raise McpError("MCP_NOT_CONNECTED", "client is not connected")
        try:
            frame = request(method, params)
            message = self._transport.send(frame, self._timeout_s)
        except TransportError as exc:
            raise McpError(
                _transport_code(exc), exc.message, retryable=exc.code == "TRANSPORT_TIMEOUT"
            ) from exc
        if message.has_error:
            error = message.error or {}
            if int(error.get("code") or 0) == -32601:
                raise McpError("MCP_NOT_FOUND", f"unknown method: {method}")
            raise McpError("MCP_PROTOCOL", f"{method} failed: {error.get('message', error)}")
        return message.result

    def list_tools(self) -> list[McpToolInfo]:
        result = self._call(METHOD_TOOLS_LIST, {"cursor": None})
        tools = _as_list(result, "tools")
        return [McpToolInfo.from_raw(t) for t in tools if isinstance(t, dict) and t.get("name")]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self._call(METHOD_TOOLS_CALL, {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            return {"content": [], "raw": result}
        if result.get("isError"):
            text = _content_text(result.get("content"))
            raise McpError("MCP_TOOL_FAILED", text or "MCP tool returned an error")
        return result

    def list_resources(self) -> list[dict[str, Any]]:
        result = self._call(METHOD_RESOURCES_LIST, None)
        return [r for r in _as_list(result, "resources") if isinstance(r, dict)]

    def read_resource(self, uri: str) -> dict[str, Any]:
        result = self._call(METHOD_RESOURCES_READ, {"uri": uri})
        return result if isinstance(result, dict) else {"raw": result}

    def list_prompts(self) -> list[dict[str, Any]]:
        result = self._call(METHOD_PROMPTS_LIST, None)
        return [p for p in _as_list(result, "prompts") if isinstance(p, dict)]

    def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self._call(METHOD_PROMPTS_GET, {"name": name, "arguments": arguments or {}})
        return result if isinstance(result, dict) else {"raw": result}


def _transport_code(exc: TransportError) -> str:
    if exc.code == "TRANSPORT_TIMEOUT":
        return "MCP_TIMEOUT"
    return "MCP_DEPENDENCY"


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


__all__ = ["McpClient", "McpError", "McpToolInfo"]
