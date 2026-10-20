"""MCP resources for the model: `mcp.resources.list` and `mcp.resources.read` (C2).

Two generic tools, by server, classified as reads (`mcp.read`). They are on
demand (`always_loaded=False`), like every MCP tool: the model finds them
with `capability.search` and runs them through `tool.call`, so the fixed tool
list stays the same whether or not servers are connected (F42). The service
registers them only while a connected server offers resources.

What a server returns is external content: the result carries its origin
(`mcp`) like any other MCP call, and binary contents are described, never
inlined.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

RESOURCES_LIST = "mcp.resources.list"
RESOURCES_READ = "mcp.resources.read"
#: Resources listed per server when no server is named.
MAX_LISTED = 100

_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "server": {
            "type": "string",
            "description": "MCP server name. Omit it to list every connected server's resources.",
        }
    },
    "additionalProperties": False,
}
_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "server": {"type": "string", "description": "MCP server name."},
        "uri": {"type": "string", "description": "Resource URI from mcp.resources.list."},
    },
    "required": ["server", "uri"],
    "additionalProperties": False,
}


def resource_tools() -> list[ToolDefinition]:
    """The two resource tools (the service decides when they are registered)."""
    common: dict[str, Any] = {
        "capabilities": ("mcp.read",),
        "risk": RISK_LOW,
        "side_effects": SIDE_EFFECT_NONE,
        "idempotent": True,
        "timeout_ms": 60_000,
        "namespace": "mcp.resources",
        "manifest": {"source": "mcp", "mcp_resources": True},
        "always_loaded": False,
        "classify": lambda arguments: ClassifiedAction(
            "mcp.read", str((arguments or {}).get("server") or "*")
        ),
    }
    return [
        ToolDefinition(
            name=RESOURCES_LIST,
            description=(
                "List the resources (files, records, documents) that connected MCP servers "
                "offer, by server: uri, name, description and mimeType."
            ),
            input_schema=_LIST_SCHEMA,
            handler=_list_handler,
            **common,
        ),
        ToolDefinition(
            name=RESOURCES_READ,
            description=(
                "Read one resource of an MCP server by its uri (from mcp.resources.list). "
                "Text comes back as text; binary contents are described, not inlined."
            ),
            input_schema=_READ_SCHEMA,
            handler=_read_handler,
            **common,
        ),
    ]


def _failure(code: ToolErrorCode, message: str, *, retryable: bool = False, details=None):
    return ToolResult(
        ok=False,
        error=ToolErrorInfo(code, message, retryable=retryable, details=details),
        origin="mcp",
    )


def _from_mcp_error(exc: Exception) -> ToolResult:
    from rinari.mcp.adapter import tool_error

    return ToolResult(ok=False, error=tool_error(exc), origin="mcp")


def _list_handler(arguments: dict, ctx) -> ToolResult:
    service = getattr(ctx, "mcp", None)
    if service is None:
        return _failure(ToolErrorCode.DEPENDENCY_ERROR, "MCP runtime is not available")
    project = getattr(ctx, "project_root", None)
    server = (arguments or {}).get("server")
    names = [server] if isinstance(server, str) and server else service.resource_servers(project)
    if not names:
        return _failure(
            ToolErrorCode.NOT_FOUND, "No connected MCP server offers resources right now."
        )
    servers: list[dict[str, Any]] = []
    for name in names:
        try:
            items = service.list_resources(name, project)
        except Exception as exc:  # McpError: reported for that server
            if len(names) == 1:
                return _from_mcp_error(exc)
            servers.append({"server": name, "error": str(getattr(exc, "message", exc))})
            continue
        servers.append(
            {
                "server": name,
                "resources": [_resource_entry(item) for item in items[:MAX_LISTED]],
                **({"more": len(items) - MAX_LISTED} if len(items) > MAX_LISTED else {}),
            }
        )
    return ToolResult(ok=True, data={"servers": servers}, origin="mcp")


def _resource_entry(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in ("uri", "name", "title", "description", "mimeType")
        if isinstance(item.get(key), str) and item.get(key)
    }


def _read_handler(arguments: dict, ctx) -> ToolResult:
    service = getattr(ctx, "mcp", None)
    if service is None:
        return _failure(ToolErrorCode.DEPENDENCY_ERROR, "MCP runtime is not available")
    server = (arguments or {}).get("server")
    uri = (arguments or {}).get("uri")
    if not isinstance(server, str) or not server or not isinstance(uri, str) or not uri:
        return _failure(ToolErrorCode.INVALID_ARGUMENT, "server and uri are required")
    try:
        result = service.read_resource(server, uri, getattr(ctx, "project_root", None))
    except Exception as exc:
        return _from_mcp_error(exc)
    contents = result.get("contents") if isinstance(result, dict) else None
    return ToolResult(
        ok=True,
        data={
            "server": server,
            "uri": uri,
            "contents": [_content(item) for item in contents or [] if isinstance(item, dict)],
        },
        origin="mcp",
    )


def _content(item: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        key: item[key] for key in ("uri", "mimeType") if isinstance(item.get(key), str)
    }
    if isinstance(item.get("text"), str):
        entry["text"] = item["text"]
    elif isinstance(item.get("blob"), str):
        try:
            size = len(base64.b64decode(item["blob"], validate=False))
        except (binascii.Error, ValueError):
            size = None
        entry["binary"] = {"bytes": size, "note": "binary content is not shown"}
    return entry


__all__ = ["RESOURCES_LIST", "RESOURCES_READ", "resource_tools"]
