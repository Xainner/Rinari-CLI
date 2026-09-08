"""Normalize MCP tools into ToolDefinitions (phase 5).

MCP servers are external capability providers; their tools are converted
into the common contract and classified through Policy Engine capabilities
(`mcp.read` when the server declares `readOnlyHint`, otherwise `mcp.call`).
The handler re-enters the McpService on invocation, so policy, approvals and
tracing see every call exactly like any other tool (harness.md 109).
"""

from __future__ import annotations

import re

from rinari.tools.definition import (
    RISK_LOW,
    RISK_MEDIUM,
    SIDE_EFFECT_NONE,
    SIDE_EFFECT_REMOTE_REVERSIBLE,
    ClassifiedAction,
    ToolDefinition,
)

from .client import McpToolInfo

_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_tool_name(raw: str) -> str:
    cleaned = _NAME_RE.sub("_", raw)
    return cleaned[:63] or "tool"


def tool_name(server: str, tool: str) -> str:
    return f"mcp.{_safe_tool_name(server)}.{_safe_tool_name(tool)}"


def mcp_tool_definitions(server: str, tools: list[McpToolInfo]) -> list[ToolDefinition]:
    definitions: list[ToolDefinition] = []
    for info in tools:
        if not info.name:
            continue
        read_only = _is_read_only(info)
        capability = "mcp.read" if read_only else "mcp.call"
        risk = RISK_LOW if read_only else RISK_MEDIUM
        side_effects = SIDE_EFFECT_NONE if read_only else SIDE_EFFECT_REMOTE_REVERSIBLE
        definitions.append(
            ToolDefinition(
                name=tool_name(server, info.name),
                description=(info.description or f"MCP tool {info.name} (server {server})").strip(),
                input_schema=info.input_schema or {"type": "object", "properties": {}},
                output_schema=info.output_schema,
                capabilities=(capability,),
                risk=risk,
                side_effects=side_effects,
                idempotent=read_only,
                timeout_ms=60_000,
                namespace=f"mcp.{_safe_tool_name(server)}",
                manifest={"source": "mcp", "server": server, "raw_name": info.name},
                # Nivel C (Etapa B): MCP tools are on-demand.
                always_loaded=False,
                classify=lambda _i, c=capability, s=server: ClassifiedAction(c, s),
                handler=_make_handler(server, info.name),
            )
        )
    return definitions


def _is_read_only(info: McpToolInfo) -> bool:
    hint = info.annotations.get("readOnlyHint")
    return hint is True


def _make_handler(server: str, raw_tool: str):
    def handler(arguments: dict, ctx) -> object:
        service = getattr(ctx, "mcp", None)
        if service is None:
            from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult

            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.DEPENDENCY_ERROR, "MCP runtime is not available"),
                origin="mcp",
            )
        from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult

        try:
            payload = service.invoke(server, raw_tool, arguments or {}, project=ctx.project_root)
        except Exception as exc:  # McpError + transport errors
            from rinari.mcp.client import McpError

            if isinstance(exc, McpError):
                code = {
                    "MCP_TIMEOUT": ToolErrorCode.TIMEOUT,
                    "MCP_NOT_CONNECTED": ToolErrorCode.DEPENDENCY_ERROR,
                    "MCP_DEPENDENCY": ToolErrorCode.DEPENDENCY_ERROR,
                    "MCP_PROTOCOL": ToolErrorCode.UNKNOWN,
                    "MCP_TOOL_FAILED": ToolErrorCode.UNKNOWN,
                    "MCP_NOT_FOUND": ToolErrorCode.NOT_FOUND,
                }.get(exc.code, ToolErrorCode.UNKNOWN)
                return ToolResult(
                    ok=False,
                    error=ToolErrorInfo(code, exc.message, retryable=exc.retryable),
                    origin="mcp",
                )
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.UNKNOWN, str(exc)),
                origin="mcp",
            )
        return ToolResult(ok=True, data=payload, origin="mcp")

    return handler


__all__ = ["mcp_tool_definitions", "tool_name"]
