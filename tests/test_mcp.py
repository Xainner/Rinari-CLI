"""Tests para la integración MCP: conexión a servidores, tools dinámicas."""

import sys
from pathlib import Path

import pytest

from rinari.mcp import (
    MCPConfigError,
    MCPConnection,
    MCPTool,
    build_openai_schemas,
    load_mcp_servers,
    to_openai_schema,
)

TEST_SERVER = Path(__file__).parent / "mcp_test_server.py"
PYTHON = sys.executable


@pytest.fixture
def mcp_config_dir(tmp_path):
    """config.toml con un servidor MCP declarado."""
    # Escapar backslashes de Windows para TOML válido
    py_escaped = PYTHON.replace("\\", "\\\\")
    server_escaped = str(TEST_SERVER).replace("\\", "\\\\")
    (tmp_path / "config.toml").write_text(
        f"""
[profile.casa]
base_url = "http://192.168.0.3:8020/v1"
model = "qwen3.6-27b"

[mcp.servers.test]
command = "{py_escaped}"
args = ["{server_escaped}"]
""",
        encoding="utf-8",
    )
    return tmp_path


def test_load_mcp_servers_parses_config(mcp_config_dir):
    servers = load_mcp_servers(mcp_config_dir)
    assert "test" in servers
    assert servers["test"].command == PYTHON
    assert TEST_SERVER.name in servers["test"].args[0]


def test_load_mcp_servers_empty_config(tmp_path):
    (tmp_path / "config.toml").write_text("[profile.casa]\nbase_url = 'x'\n", encoding="utf-8")
    servers = load_mcp_servers(tmp_path)
    assert servers == {}


def test_mcp_connection_lists_tools(mcp_config_dir):
    servers = load_mcp_servers(mcp_config_dir)
    with MCPConnection(servers["test"]) as conn:
        tools = conn.list_tools()
        names = {t.name for t in tools}
        assert "echo" in names
        assert "add" in names


def test_mcp_connection_calls_tool(mcp_config_dir):
    servers = load_mcp_servers(mcp_config_dir)
    with MCPConnection(servers["test"]) as conn:
        result = conn.call_tool("echo", {"text": "hola"})
        assert "echo: hola" in str(result)


def test_mcp_connection_call_add(mcp_config_dir):
    servers = load_mcp_servers(mcp_config_dir)
    with MCPConnection(servers["test"]) as conn:
        result = conn.call_tool("add", {"a": 2, "b": 3})
        assert "5" in str(result)


def test_to_openai_schema_converts_mcp_tool():
    tool = MCPTool(
        name="echo",
        description="Devuelve el texto",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    schema = to_openai_schema(tool)
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "echo"
    assert schema["function"]["parameters"]["properties"]["text"]["type"] == "string"


def test_build_openai_schemas():
    tools = [
        MCPTool(name="echo", description="d", input_schema={"type": "object", "properties": {}}),
        MCPTool(name="add", description="d", input_schema={"type": "object", "properties": {}}),
    ]
    schemas = build_openai_schemas(tools)
    assert len(schemas) == 2
    assert {s["function"]["name"] for s in schemas} == {"echo", "add"}


def test_mcp_config_missing_command(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[mcp.servers.rotos]\nargs = ["x"]\n', encoding="utf-8"
    )
    with pytest.raises(MCPConfigError):
        load_mcp_servers(tmp_path)


def test_mcp_connection_bad_server():
    """Servidor que no arranca → error claro."""
    from rinari.mcp import MCPServer

    bad = MCPServer(name="bad", command=PYTHON, args=["/no/existe.py"])
    with pytest.raises(Exception):
        with MCPConnection(bad) as conn:
            conn.list_tools()
