"""MCP JSON-RPC 2.0 message encoding/parsing (version-independent).

Newline-delimited JSON over stdio is the MCP stdio transport framing.
Each frame is a single JSON object; a batch (JSON array) is also allowed by
JSON-RPC 2.0 but MCP servers send single messages, so we handle both.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from typing import Any

# MCP method names (stable across the protocol).
METHOD_INITIALIZE = "initialize"
METHOD_INITIALIZED = "notifications/initialized"
METHOD_TOOLS_LIST = "tools/list"
METHOD_TOOLS_CALL = "tools/call"
METHOD_RESOURCES_LIST = "resources/list"
METHOD_RESOURCES_READ = "resources/read"
METHOD_PROMPTS_LIST = "prompts/list"
METHOD_PROMPTS_GET = "prompts/get"

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "rinari", "version": "1.0.0"}

# Monotonic request id source.
_id_counter = itertools.count(1)


def next_id() -> int:
    return next(_id_counter)


@dataclass(frozen=True, slots=True)
class McpMessage:
    """A decoded JSON-RPC 2.0 object (request, response, or notification)."""

    raw: dict[str, Any]

    # -- classification ---------------------------------------------------

    @property
    def id(self) -> Any:
        return self.raw.get("id")

    @property
    def has_result(self) -> bool:
        return "result" in self.raw

    @property
    def has_error(self) -> bool:
        return "error" in self.raw

    @property
    def is_response(self) -> bool:
        return self.id is not None and (self.has_result or self.has_error)

    @property
    def method(self) -> str | None:
        return self.raw.get("method")

    @property
    def result(self) -> Any:
        return self.raw.get("result")

    @property
    def error(self) -> dict[str, Any] | None:
        return self.raw.get("error")

    @property
    def params(self) -> dict[str, Any]:
        return self.raw.get("params") or {}


def parse_message(line: str) -> McpMessage | None:
    """Parse one framed line into an McpMessage, or None if not a frame."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (ValueError, json.JSONDecodeError):
        return None
    if isinstance(obj, list):
        # Batch: MCP does not use them; represent the first element.
        obj = obj[0] if obj else None
    if not isinstance(obj, dict):
        return None
    return McpMessage(raw=obj)


def request(
    method: str,
    params: dict[str, Any] | None = None,
    req_id: int | None = None,
) -> str:
    """Serialize a JSON-RPC 2.0 request (with id) to a single frame."""
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": req_id if req_id is not None else next_id(),
        "method": method,
    }
    if params is not None:
        payload["params"] = params
    return json.dumps(payload, separators=(",", ":"))


def notification(method: str, params: dict[str, Any] | None = None) -> str:
    """Serialize a JSON-RPC 2.0 notification (no id)."""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    return json.dumps(payload, separators=(",", ":"))


def initialize_params() -> dict[str, Any]:
    return {
        "protocolVersion": _PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": _CLIENT_INFO,
    }


__all__ = [
    "METHOD_INITIALIZE",
    "METHOD_INITIALIZED",
    "METHOD_PROMPTS_GET",
    "METHOD_PROMPTS_LIST",
    "METHOD_RESOURCES_LIST",
    "METHOD_RESOURCES_READ",
    "METHOD_TOOLS_CALL",
    "METHOD_TOOLS_LIST",
    "McpMessage",
    "initialize_params",
    "next_id",
    "notification",
    "parse_message",
    "request",
]
