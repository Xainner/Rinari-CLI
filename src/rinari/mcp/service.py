"""McpService: server registry + connection cache + tool normalization.

Application-level facade over the `mcp_servers` table and the McpClient.
Connections are lazy (first use) and cached per process; `disconnect` drops
them. Project-scope servers require project trust before any connection.

Secrets: server `config` may map env-var names to `env://PROCESS_VAR`
references (resolved from the *process* environment at spawn time). Plain
values are rejected so no secret ever lands in the state DB (AGENTS.md 10).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from rinari.application.context import AppContext
from rinari.shared.clock import now_iso
from rinari.trust import TrustService

from .adapter import mcp_tool_definitions
from .client import McpClient, McpError, McpToolInfo
from .transport import InProcessTransport, McpTransport, StdioTransport

TRANSPORT_STDLIO = "stdio"
TRANSPORTS = (TRANSPORT_STDLIO,)


def _now(ctx: AppContext) -> str:
    return now_iso(ctx.clock)


MAX_LOG_ENTRIES = 100


class McpService:
    def __init__(self, ctx: AppContext, trust: TrustService) -> None:
        self._ctx = ctx
        self._trust = trust
        self._clients: dict[str, McpClient] = {}
        self._logs: list[dict] = []

    def _log_entry(self, name: str, level: str, message: str) -> None:
        self._logs.append(
            {"at": _now(self._ctx), "server": name, "level": level, "message": message}
        )
        if len(self._logs) > MAX_LOG_ENTRIES:
            del self._logs[: len(self._logs) - MAX_LOG_ENTRIES]

    # -- registry -------------------------------------------------------------

    def add(
        self,
        name: str,
        command: list[str],
        *,
        scope: str = "global",
        env_refs: dict[str, str] | None = None,
        transport: str = TRANSPORT_STDLIO,
    ) -> dict:
        if transport not in TRANSPORTS:
            raise ValueError(f"unsupported MCP transport: {transport!r} (v1: stdio)")
        if not command:
            raise ValueError("stdio MCP servers need a command (e.g. ['npx', '-y', '...'])")
        config = {}
        if env_refs:
            for key, ref in env_refs.items():
                if not (isinstance(ref, str) and ref.startswith("env://")):
                    raise ValueError(
                        f"env ref for {key!r} must be 'env://PROCESS_VAR' "
                        "(plain values are never stored)"
                    )
            config["env"] = dict(env_refs)
        row = self._ctx.mcp_server_repo.add(
            self._ctx.ids.new("mcp"),
            name=name,
            transport=transport,
            command=" ".join(command) if transport == TRANSPORT_STDLIO else "",
            url="",
            scope=scope,
            config_json=json.dumps(config, sort_keys=True),
            created_at=_now(self._ctx),
        )
        return row

    def remove(self, name: str, scope: str = "global") -> bool:
        self._clients.pop(name, None)
        return self._ctx.mcp_server_repo.delete(name, scope)

    def enable(self, name: str, scope: str = "global") -> dict | None:
        ok = self._ctx.mcp_server_repo.set_enabled(name, scope, True, _now(self._ctx))
        return self._ctx.mcp_server_repo.find(name, scope) if ok else None

    def disable(self, name: str, scope: str = "global") -> dict | None:
        ok = self._ctx.mcp_server_repo.set_enabled(name, scope, False, _now(self._ctx))
        row = self._ctx.mcp_server_repo.find(name, scope)
        if ok and row is not None:
            self._clients.pop(name, None)
        return row if ok else None

    def list(self, scope: str | None = None) -> list[dict]:
        return self._ctx.mcp_server_repo.list(scope)

    def show(self, name: str, scope: str = "global") -> dict | None:
        return self._ctx.mcp_server_repo.find(name, scope)

    # -- connections -------------------------------------------------------------

    def _trusted(self, row: dict, project: Path | None) -> bool:
        if row.get("scope") != "project":
            return True
        return project is not None and self._trust.is_trusted(project)

    def _config(self, row: dict) -> dict:
        try:
            config = json.loads(row.get("config_json") or "{}")
        except json.JSONDecodeError:
            config = {}
        return config if isinstance(config, dict) else {}

    def _transport_for(self, row: dict) -> McpTransport:
        config = self._config(row)
        if row["transport"] == TRANSPORT_STDLIO:
            command_parts = [p for p in row.get("command", "").split() if p]
            env_refs = config.get("env") or {}
            env: dict[str, str] = {}
            for key, ref in env_refs.items():
                source = ref.split("://", 1)[1] if "://" in ref else ref
                value = os.environ.get(source)
                if value is not None:
                    env[key] = value
            return StdioTransport(command_parts, env=env or None)
        raise McpError("MCP_DEPENDENCY", f"unsupported transport: {row['transport']}")

    def _client(self, name: str, project: Path | None = None) -> McpClient:
        cached = self._clients.get(name)
        if cached is not None and cached.connected:
            return cached
        row = None
        for scope in ("global", "project"):
            row = self._ctx.mcp_server_repo.find(name, scope)
            if row is not None:
                break
        if row is None:
            raise McpError("MCP_NOT_FOUND", f"unknown MCP server: {name}")
        if not row.get("enabled"):
            raise McpError("MCP_NOT_CONNECTED", f"server {name} is disabled")
        if not self._trusted(row, project):
            raise McpError(
                "MCP_NOT_CONNECTED",
                f"server {name} is project-scoped and the project is not trusted",
            )
        if cached is not None:
            cached.close()
        client = McpClient(self._transport_for(row))
        self._clients[name] = client
        try:
            client.connect()
        except McpError as exc:
            self._log_entry(name, "error", f"connect failed: {exc.message}")
            raise
        self._log_entry(name, "info", f"connected ({row['transport']})")
        return client

    def connect(self, name: str, project: Path | None = None) -> dict | None:
        client = self._client(name, project)
        info = client.server_info or {}
        return {"name": name, "connected": True, "serverInfo": info}

    def client(self, name: str, project: Path | None = None) -> McpClient:
        """Public accessor for a connected client (introspection commands)."""
        return self._client(name, project)

    def disconnect(self, name: str) -> bool:
        client = self._clients.pop(name, None)
        if client is None:
            return False
        client.close()
        return True

    # -- capabilities -------------------------------------------------------------

    def tools(self, name: str, project: Path | None = None) -> list:
        client = self._client(name, project)
        infos: list[McpToolInfo] = client.list_tools()
        return mcp_tool_definitions(name, infos)

    def invoke(
        self,
        name: str,
        tool: str,
        arguments: dict,
        project: Path | None = None,
    ) -> dict:
        client = self._client(name, project)
        result = client.call_tool(tool, arguments)
        payload: dict = {"content": _content_summary(result.get("content"))}
        if isinstance(result.get("structuredContent"), (dict, list)):
            payload["structuredContent"] = result["structuredContent"]
        return payload

    def logs(self, name: str | None = None, limit: int = 50) -> list[dict]:
        entries = self._logs
        if name is not None:
            entries = [e for e in entries if e["server"] == name]
        return list(reversed(entries))[-limit:]

    # -- testing -----------------------------------------------------------------

    def test(self, name: str, project: Path | None = None) -> dict:
        try:
            client = self._client(name, project)
        except McpError as exc:
            return {"ok": False, "error": exc.code, "message": exc.message, "tools": 0}
        try:
            tools = client.list_tools()
        except McpError as exc:
            return {"ok": False, "error": exc.code, "message": exc.message, "tools": 0}
        info = client.server_info or {}
        return {
            "ok": True,
            "server": name,
            "serverInfo": info,
            "tools": len(tools),
            "names": [t.name for t in tools],
        }


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


__all__ = ["TRANSPORTS", "TRANSPORT_STDLIO", "InProcessTransport", "McpService"]
