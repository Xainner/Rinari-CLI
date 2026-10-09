"""Ecosystem reads/writes for desktop clients (Phase 9).

Thin adapters over McpService, PluginService, the native tool registry and
the policy mode mapping. Secrets never cross: MCP rows carry only secret
references (`env://`, or slots the CredentialStore owns), and the MCP view
reports `configured` flags and env-var names, never values. This module adds
no new subprocess or filesystem powers beyond what the owning services
already expose to the CLI.
"""

from __future__ import annotations

from typing import Any


def mcp_row_view(
    row: dict, connected: bool, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    """MCP server for clients. `details` is `McpService.view(row)`.

    Keys from `details`: url, argv, timeout_s, auth {kind, token?}, headers
    [{name, secret, configured, value?|source, env_var?}], env [{name,
    configured, source, env_var?}], warnings. Never a secret value.
    """
    view: dict[str, Any] = {
        "name": row.get("name"),
        "transport": row.get("transport"),
        "command": row.get("command") or "",
        "scope": row.get("scope") or "global",
        "enabled": bool(row.get("enabled")),
        "connected": connected,
        "updated_at": row.get("updated_at"),
    }
    if details:
        view.update(details)
    return view


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
    from rinari.tools.availability import availability

    return {
        "name": tool.name,
        "description": tool.description,
        "capabilities": list(tool.capabilities) or None,
        "permissions": list(tool.permissions) or None,
        "risk": tool.risk,
        "side_effects": tool.side_effects,
        "concurrency": tool.concurrency,
        "namespace": tool.namespace,
        "always_loaded": tool.always_loaded,
        "availability": availability(tool.name),
        "input_schema": tool.input_schema,
        "output_schema": tool.output_schema,
    }
