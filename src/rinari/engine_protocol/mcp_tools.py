"""`mcp.tools.*`: permissions per connector and per tool (plan de conectores, C5).

- `mcp.tools.list {name, scope?}`: the server's tools with their
  description, the server's annotations, the owner's corrections, the hints
  that count, the owner's setting, the effective decision and where it comes
  from (`tool`, `server` or `mode`), and what each mode does with the tool.
  Tools come from the server's last `tools/list` (nothing connects here);
  a tool with a saved setting the server no longer lists is shown with
  `listed: false`.
- `mcp.tools.set {name, tool, decision?, annotations?, scope?}`: Allow / Ask /
  Block for one tool (`null` follows the server's default) and corrections
  of its hints (`{"readOnlyHint": true}`, `null` back to the server's).
- `mcp.tools.set_default {name, decision, scope?}`: the server's Allow / Ask
  / Block for every tool without its own setting (`null`: the mode decides).

Capability `mcp_tool_permissions_v1`. Nothing here carries a secret.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.mcp.adapter import facts_of, tool_name
from rinari.mcp.permissions import effective_decision, tool_facts
from rinari.policy.engine import PermissionProfile, PolicyEngine, SessionScope
from rinari.policy.mcp_tools import ANNOTATION_KEYS, McpToolFacts
from rinari.shared.errors import NotFoundError

_SCOPES = ("global", "project")


class McpToolMethods:
    def __init__(self, services) -> None:
        self._mcp = services.mcp
        # The rules only: no network policy is consulted for MCP calls.
        self._policy = PolicyEngine()

    def register(self, dispatcher) -> None:
        dispatcher.register("mcp.tools.list", self.list)
        dispatcher.register("mcp.tools.set", self.set)
        dispatcher.register("mcp.tools.set_default", self.set_default)

    # -- helpers ---------------------------------------------------------------------

    def _row(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise EngineProtocolError(INVALID_PARAMS, "Param 'name' is required.")
        scope = params.get("scope")
        if scope is not None and scope not in _SCOPES:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'scope' must be global or project.")
        if scope is None:
            row = self._mcp.show(name) or self._mcp.show(name, "project")
        else:
            row = self._mcp.show(name, scope)
        if row is None:
            raise NotFoundError(f"Unknown MCP server: {name}.")
        return row

    def _by_mode(self, facts: McpToolFacts) -> dict[str, str]:
        """What each mode does with a call, before any external content."""
        capability = "mcp.read" if facts.read_only else "mcp.call"
        out: dict[str, str] = {}
        for profile in PermissionProfile:
            scope = SessionScope(kind="CHAT", root=None, cwd=Path("."), profile=profile)
            out[profile.value] = self._policy.decide(capability, scope, mcp=facts).action.value
        return out

    def _tool_view(
        self,
        server: str,
        raw: str,
        server_annotations: dict[str, Any],
        description: str,
        permissions: dict[str, Any],
        *,
        listed: bool,
    ) -> dict[str, Any]:
        facts = tool_facts(server, raw, server_annotations, permissions)
        entry = (permissions.get("tools") or {}).get(raw) or {}
        decision, source = effective_decision(permissions, raw)
        hints = {
            key: server_annotations.get(key)
            if isinstance(server_annotations.get(key), bool)
            else None
            for key in ANNOTATION_KEYS
        }
        effective = {
            "readOnlyHint": facts.read_only,
            "destructiveHint": facts.destructive,
            "openWorldHint": facts.open_world,
        }
        title = server_annotations.get("title")
        return {
            "name": raw,
            "tool": tool_name(server, raw),
            "title": title if isinstance(title, str) else None,
            "description": description,
            "listed": listed,
            "annotations": {
                "server": hints,
                "owner": dict(entry.get("annotations") or {}),
                "effective": effective,
            },
            "kind": "read" if facts.read_only else "destructive" if facts.destructive else "write",
            "sends_out": facts.sends_out,
            "setting": entry.get("decision"),
            "decision": decision,
            "decision_source": source,
            "by_mode": self._by_mode(facts),
        }

    def _server_view(self, row: dict[str, Any]) -> dict[str, Any]:
        name = row["name"]
        permissions = self._mcp.permissions(name, row.get("scope") or "global")
        tools: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tool in self._mcp.known_tools(name):
            manifest = tool.manifest or {}
            raw = manifest.get("raw_name")
            if not isinstance(raw, str) or facts_of(tool) is None or raw in seen:
                continue
            seen.add(raw)
            tools.append(
                self._tool_view(
                    name,
                    raw,
                    dict(manifest.get("annotations") or {}),
                    str(manifest.get("server_description") or ""),
                    permissions,
                    listed=True,
                )
            )
        for raw in sorted(set(permissions.get("tools") or {}) - seen):
            tools.append(self._tool_view(name, raw, {}, "", permissions, listed=False))
        status = self._mcp.status(row)
        return {
            "name": name,
            "scope": row.get("scope") or "global",
            "status": status.get("status"),
            "default": permissions.get("default"),
            "tools": tools,
        }

    # -- methods ---------------------------------------------------------------------

    def list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"server": self._server_view(self._row(params or {}))}

    def set(self, params: dict[str, Any]) -> dict[str, Any]:
        params = params or {}
        row = self._row(params)
        tool = params.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise EngineProtocolError(INVALID_PARAMS, "Param 'tool' is required.")
        changes = {key: params[key] for key in ("decision", "annotations") if key in params}
        if not changes:
            raise EngineProtocolError(
                INVALID_PARAMS, "Nothing to change: pass 'decision' and/or 'annotations'."
            )
        try:
            self._mcp.set_tool_permission(row["name"], tool, scope=row.get("scope"), **changes)
        except LookupError:
            raise NotFoundError(f"Unknown MCP server: {row['name']}.") from None
        except ValueError as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from exc
        server = self._server_view(self._row(params))
        view = next((item for item in server["tools"] if item["name"] == tool), None)
        if view is None:
            # Back to the server's hints and the mode: nothing is kept for it.
            permissions = self._mcp.permissions(row["name"], row.get("scope") or "global")
            view = self._tool_view(row["name"], tool, {}, "", permissions, listed=False)
        return {"server": server, "tool": view}

    def set_default(self, params: dict[str, Any]) -> dict[str, Any]:
        params = params or {}
        row = self._row(params)
        if "decision" not in params:
            raise EngineProtocolError(
                INVALID_PARAMS, "Param 'decision' is required (allow, ask, deny or null)."
            )
        try:
            self._mcp.set_default_permission(
                row["name"], params["decision"], scope=row.get("scope")
            )
        except LookupError:
            raise NotFoundError(f"Unknown MCP server: {row['name']}.") from None
        except ValueError as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from exc
        return {"server": self._server_view(self._row(params))}


__all__ = ["McpToolMethods"]
