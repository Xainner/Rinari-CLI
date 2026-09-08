"""Ecosystem reads/writes for desktop clients (Phase 9).

Thin adapters over McpService, PluginService, the native tool registry and
the policy mode mapping. Secrets never cross: MCP rows carry only `env://`
references (enforced at add-time by McpService itself), and this module
adds no new subprocess or filesystem powers beyond what the owning
services already expose to the CLI.
"""

from __future__ import annotations

from typing import Any


def mcp_row_view(row: dict, connected: bool) -> dict[str, Any]:
    return {
        "name": row.get("name"),
        "transport": row.get("transport"),
        "command": row.get("command") or "",
        "scope": row.get("scope") or "global",
        "enabled": bool(row.get("enabled")),
        "connected": connected,
        "updated_at": row.get("updated_at"),
    }


def plugin_row_view(row: dict, diagnostics: list[dict] | None = None) -> dict[str, Any]:
    return {
        "name": row.get("name"),
        "version": row.get("version"),
        "source": row.get("source"),
        "scope": row.get("scope"),
        "enabled": bool(row.get("enabled")),
        "path": row.get("path"),
        "capabilities": row.get("capabilities") or [],
        "diagnostics": diagnostics if diagnostics is not None else [],
    }


def tool_row_view(tool: Any) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "capabilities": list(tool.capabilities) or None,
        "permissions": list(tool.permissions) or None,
        "risk": tool.risk,
        "side_effects": tool.side_effects,
        "namespace": tool.namespace,
        "always_loaded": tool.always_loaded,
    }
