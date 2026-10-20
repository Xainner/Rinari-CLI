"""Normalize MCP tools into ToolDefinitions (phase 5).

MCP servers are external capability providers; their tools are converted
into the common contract and classified through Policy Engine capabilities
(`mcp.read` for a declared read, otherwise `mcp.call`). The handler re-enters
the McpService on invocation, so policy, approvals and tracing see every call
exactly like any other tool (harness.md 109).

The annotations count (plan de conectores, C5): `readOnlyHint` is a read,
`destructiveHint` is high risk and `openWorldHint` says whether a call sends
data outside. The owner's corrections and Allow / Ask / Block
(`mcp/permissions.py`) are applied here: each action carries its
`McpToolFacts`, and `with_permissions` rebuilds the definitions when the
owner changes a setting.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from rinari.policy.mcp_tools import McpToolFacts
from rinari.tools.definition import (
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    SIDE_EFFECT_NONE,
    SIDE_EFFECT_REMOTE_DESTRUCTIVE,
    SIDE_EFFECT_REMOTE_REVERSIBLE,
    ClassifiedAction,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

from .client import McpToolInfo
from .permissions import tool_facts

_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_tool_name(raw: str) -> str:
    cleaned = _NAME_RE.sub("_", raw)
    return cleaned[:63] or "tool"


def tool_name(server: str, tool: str) -> str:
    return f"mcp.{_safe_tool_name(server)}.{_safe_tool_name(tool)}"


def mcp_tool_definitions(
    server: str,
    tools: list[McpToolInfo],
    permissions: Mapping[str, Any] | None = None,
) -> list[ToolDefinition]:
    """The server's tools, classified with their annotations and the owner's
    `permissions` (`mcp/permissions.py`; None: nothing set)."""
    definitions: list[ToolDefinition] = []
    for info in tools:
        if not info.name:
            continue
        definition = ToolDefinition(
            name=tool_name(server, info.name),
            description=(info.description or f"MCP tool {info.name} (server {server})").strip(),
            input_schema=info.input_schema or {"type": "object", "properties": {}},
            output_schema=info.output_schema,
            timeout_ms=60_000,
            namespace=f"mcp.{_safe_tool_name(server)}",
            manifest={
                "source": "mcp",
                "server": server,
                "raw_name": info.name,
                # The server's own hints, as it sent them (C5 views and rebuilds).
                "annotations": dict(info.annotations or {}),
                "server_description": info.description or "",
            },
            # Nivel C (Etapa B): MCP tools are on-demand.
            always_loaded=False,
            handler=_make_handler(server, info.name),
        )
        definitions.append(_classified(definition, permissions or {}))
    return definitions


def with_permissions(
    definitions: list[ToolDefinition], permissions: Mapping[str, Any] | None
) -> list[ToolDefinition]:
    """The same tools classified again after the owner changed a setting."""
    return [
        _classified(tool, permissions or {}) if (tool.manifest or {}).get("raw_name") else tool
        for tool in definitions
    ]


def facts_of(tool: ToolDefinition) -> McpToolFacts | None:
    """What the policy knows about an MCP tool (None for any other tool)."""
    try:
        action = tool.classify_action({})
    except Exception:  # pragma: no cover - MCP classifiers never read the input
        return None
    return getattr(action, "mcp", None)


def _classified(tool: ToolDefinition, permissions: Mapping[str, Any]) -> ToolDefinition:
    manifest = tool.manifest or {}
    server = str(manifest.get("server") or "")
    facts = tool_facts(server, str(manifest["raw_name"]), manifest.get("annotations"), permissions)
    capability = "mcp.read" if facts.read_only else "mcp.call"
    if facts.read_only:
        risk, side_effects = RISK_LOW, SIDE_EFFECT_NONE
    elif facts.destructive:
        risk, side_effects = RISK_HIGH, SIDE_EFFECT_REMOTE_DESTRUCTIVE
    else:
        risk, side_effects = RISK_MEDIUM, SIDE_EFFECT_REMOTE_REVERSIBLE
    return replace(
        tool,
        capabilities=(capability,),
        risk=risk,
        side_effects=side_effects,
        idempotent=facts.read_only,
        classify=lambda _i, c=capability, s=server, f=facts: ClassifiedAction(c, s, mcp=f),
    )


_ERROR_CODES = {
    "MCP_TIMEOUT": ToolErrorCode.TIMEOUT,
    "MCP_NOT_CONNECTED": ToolErrorCode.DEPENDENCY_ERROR,
    "MCP_DEPENDENCY": ToolErrorCode.DEPENDENCY_ERROR,
    "MCP_UNREACHABLE": ToolErrorCode.NETWORK_ERROR,
    "MCP_AUTH_REJECTED": ToolErrorCode.AUTH_REQUIRED,
    "MCP_PROTOCOL": ToolErrorCode.UNKNOWN,
    "MCP_TOOL_FAILED": ToolErrorCode.UNKNOWN,
    "MCP_NOT_FOUND": ToolErrorCode.NOT_FOUND,
}


def tool_error(exc: Exception) -> ToolErrorInfo:
    """A failed MCP call as a tool error; the server's hint goes in `details`.

    `reauth_required` (C1: renewing the sign-in failed) is said plainly, so
    the model tells the owner to reconnect instead of trying another way.
    """
    from rinari.mcp.client import McpError

    if not isinstance(exc, McpError):
        return ToolErrorInfo(ToolErrorCode.UNKNOWN, str(exc))
    if exc.hint == "reauth_required":
        return ToolErrorInfo(
            ToolErrorCode.AUTH_EXPIRED,
            f"{exc.message} The owner has to reconnect this MCP server in Settings.",
            details={"hint": exc.hint},
        )
    return ToolErrorInfo(
        _ERROR_CODES.get(exc.code, ToolErrorCode.UNKNOWN),
        exc.message,
        retryable=exc.retryable,
        details={"hint": exc.hint} if exc.hint else None,
    )


def _make_handler(server: str, raw_tool: str):
    def handler(arguments: dict, ctx) -> object:
        service = getattr(ctx, "mcp", None)
        if service is None:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.DEPENDENCY_ERROR, "MCP runtime is not available"),
                origin="mcp",
            )
        try:
            payload = service.invoke(server, raw_tool, arguments or {}, project=ctx.project_root)
        except Exception as exc:  # McpError + transport errors
            return ToolResult(ok=False, error=tool_error(exc), origin="mcp")
        return ToolResult(ok=True, data=payload, origin="mcp")

    return handler


__all__ = ["facts_of", "mcp_tool_definitions", "tool_error", "tool_name", "with_permissions"]
