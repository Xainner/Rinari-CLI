"""Integración MCP (Model Context Protocol) para Rinari.

Los servidores MCP se declaran en config.toml:

    [mcp.servers.nombre]
    command = "python"          # o ruta al ejecutable
    args = ["/path/al/server.py"]

El agente puede entonces usar las tools expuestas por esos servidores
como si fueran tools nativas. La conexión es por stdio (el estándar MCP).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11
    import tomli as tomllib  # type: ignore


class MCPConfigError(Exception):
    """Error de configuración MCP."""


@dataclass
class MCPServer:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class MCPTool:
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)


def load_mcp_servers(config_dir: Path | str | None = None) -> dict[str, MCPServer]:
    """Lee los servidores MCP de config.toml (sección [mcp.servers.<name>])."""
    config_dir = Path(config_dir) if config_dir else Path.home() / ".rinari"
    path = config_dir / "config.toml"
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    mcp_cfg = data.get("mcp", {})
    servers_cfg = mcp_cfg.get("servers", {})
    servers: dict[str, MCPServer] = {}
    for name, raw in servers_cfg.items():
        if not isinstance(raw, dict):
            continue
        command = raw.get("command")
        if not command:
            raise MCPConfigError(f"Servidor MCP '{name}': falta 'command'")
        servers[name] = MCPServer(
            name=name,
            command=str(command),
            args=[str(a) for a in raw.get("args", [])],
            env={str(k): str(v) for k, v in raw.get("env", {}).items()},
        )
    return servers


def to_openai_schema(tool: MCPTool) -> dict:
    """Convierte un tool MCP a schema OpenAI function calling."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema or {"type": "object", "properties": {}},
        },
    }


def build_openai_schemas(tools: list[MCPTool]) -> list[dict]:
    return [to_openai_schema(t) for t in tools]


class MCPConnection:
    """Conexión stdio a un servidor MCP.

    Uso (context manager para cerrar limpiamente):

        with MCPConnection(server) as conn:
            tools = conn.list_tools()
            result = conn.call_tool("echo", {"text": "hola"})
    """

    def __init__(self, server: MCPServer):
        self.server = server
        self._session = None
        self._stack = None
        # Un solo event loop para toda la conexión: asyncio.run() crea un loop
        # nuevo por llamada y el AsyncExitStack no puede cerrarse en otro loop.
        self._loop = asyncio.new_event_loop()

    def __enter__(self) -> "MCPConnection":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._stack is not None:
            try:
                self._run(self._stack.aclose())
            except Exception:  # noqa: BLE001
                pass
            self._stack = None
            self._session = None
        try:
            self._loop.close()
        except Exception:  # noqa: BLE001
            pass

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _ensure_connected(self):
        if self._session is not None:
            return
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        # Limpiar PYTHONPATH heredado (p.ej. del venv de Hermes) para que el
        # server MCP use su propio intérprete/site-packages, no uno ajeno.
        import os as _os

        env = dict(_os.environ)
        env.pop("PYTHONPATH", None)
        if self.server.env:
            env.update(self.server.env)

        params = StdioServerParameters(
            command=self.server.command,
            args=self.server.args,
            env=env,
        )

        async def _connect():
            stack = __import__("contextlib").AsyncExitStack()
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return stack, session

        self._stack, self._session = self._run(_connect())

    def list_tools(self) -> list[MCPTool]:
        self._ensure_connected()
        result = self._run(self._session.list_tools())
        tools = []
        for t in result.tools:
            tools.append(
                MCPTool(
                    name=t.name,
                    description=t.description or "",
                    input_schema=t.inputSchema or {},
                )
            )
        return tools

    def call_tool(self, name: str, arguments: dict) -> str:
        self._ensure_connected()
        result = self._run(self._session.call_tool(name, arguments or {}))
        parts = []
        if hasattr(result, "content"):
            for c in result.content:
                if getattr(c, "type", "") == "text":
                    parts.append(getattr(c, "text", ""))
                else:
                    parts.append(str(c))
        if hasattr(result, "isError") and result.isError:
            return json.dumps({"error": parts}, ensure_ascii=False)
        return "\n".join(parts) if parts else str(result)
