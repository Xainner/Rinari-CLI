"""Tests de integración: MCP como tools dinámicas del agente."""

import sys
from pathlib import Path

import pytest

from rinari.agent.loop import run_agent
from rinari.mcp import MCPConnection, MCPTool, build_openai_schemas, load_mcp_servers

TEST_SERVER = Path(__file__).parent / "mcp_test_server.py"
PYTHON = sys.executable


@pytest.fixture
def mcp_config(tmp_path):
    py_escaped = PYTHON.replace("\\", "\\\\")
    server_escaped = str(TEST_SERVER).replace("\\", "\\\\")
    (tmp_path / "config.toml").write_text(
        f"""
[mcp.servers.test]
command = "{py_escaped}"
args = ["{server_escaped}"]
""",
        encoding="utf-8",
    )
    return tmp_path


class ScriptedClient:
    """Cliente falso: responde con tool call MCP y luego respuesta final."""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []
        self.tools_seen = None

    def chat(self, messages, temperature=0.7, max_tokens=None, tools=None):
        self.requests.append(list(messages))
        if tools is not None and self.tools_seen is None:
            self.tools_seen = [t["function"]["name"] for t in tools]
        return self.responses.pop(0)


def mcp_tool_call(name: str, args: dict) -> dict:
    import json

    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "mcp_1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def test_mcp_tools_exposed_to_model(mcp_config):
    """El agente expone las tools de los servidores MCP al modelo."""
    servers = load_mcp_servers(mcp_config)
    assert "test" in servers

    with MCPConnection(servers["test"]) as conn:
        mcp_tools = conn.list_tools()
        schemas = build_openai_schemas(mcp_tools)

    client = ScriptedClient(
        [
            mcp_tool_call("echo", {"text": "hola"}),
            {"role": "assistant", "content": "listo"},
        ]
    )
    result = run_agent(
        task="usa la tool echo",
        client=client,
        cwd=str(mcp_config),
        auto_approve=True,
        max_iterations=5,
        mcp_servers=servers,
    )
    # El modelo vio las tools MCP (echo y add)
    assert "echo" in client.tools_seen
    assert "add" in client.tools_seen
    assert result["status"] == "done"


def test_mcp_tool_call_executed_via_connection(mcp_config):
    """La tool MCP se ejecuta contra el server real y el resultado vuelve al modelo."""
    import json

    servers = load_mcp_servers(mcp_config)
    client = ScriptedClient(
        [
            mcp_tool_call("echo", {"text": "hola desde mcp"}),
            {"role": "assistant", "content": "echo ejecutado"},
        ]
    )
    result = run_agent(
        task="prueba echo",
        client=client,
        cwd=str(mcp_config),
        auto_approve=True,
        max_iterations=5,
        mcp_servers=servers,
    )
    # El resultado del tool MCP se devolvió al modelo en la 2ª llamada
    second = client.requests[1]
    tool_msg = [m for m in second if m["role"] == "tool"]
    assert tool_msg, "No se devolvió mensaje tool al modelo"
    assert "echo: hola desde mcp" in tool_msg[0]["content"]
    assert result["status"] == "done"


def test_mcp_add_tool_returns_number(mcp_config):
    """La tool add devuelve el resultado numérico al modelo."""
    servers = load_mcp_servers(mcp_config)
    client = ScriptedClient(
        [
            mcp_tool_call("add", {"a": 2, "b": 40}),
            {"role": "assistant", "content": "suma hecha"},
        ]
    )
    run_agent(
        task="suma 2 y 40",
        client=client,
        cwd=str(mcp_config),
        auto_approve=True,
        max_iterations=5,
        mcp_servers=servers,
    )
    second = client.requests[1]
    tool_msg = [m for m in second if m["role"] == "tool"]
    assert "42" in tool_msg[0]["content"]


def test_agent_without_mcp_config_runs_normal(tmp_path):
    """Sin servidores MCP configurados, el agente funciona como siempre."""
    client = ScriptedClient([{"role": "assistant", "content": "ok"}])
    result = run_agent(
        task="tarea normal",
        client=client,
        cwd=str(tmp_path),
        auto_approve=True,
        max_iterations=5,
    )
    assert result["status"] == "done"
    assert client.tools_seen is not None
