"""MCP client/runtime tests (phase 5).

Deterministic: InProcessTransport (no subprocess), no network, FakeClock.
Covers protocol framing, client handshake/methods/errors, ToolDefinition
normalization (namespacing, read-only hints), and McpService registry +
trust gate + invocation path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rinari.application.services import build_services
from rinari.mcp.adapter import mcp_tool_definitions, tool_name
from rinari.mcp.client import McpClient, McpError, McpToolInfo
from rinari.mcp.protocol import McpMessage, notification, parse_message, request
from rinari.mcp.service import McpService
from rinari.mcp.transport import InProcessTransport
from rinari.tools.definition import ToolContext, ToolErrorCode

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


class FakeMcpServer:
    """Scripted in-memory MCP server (tools/list, tools/call)."""

    def __init__(
        self,
        tools: list[dict] | None = None,
        calls: dict[str, str] | None = None,
        error_names: set[str] | None = None,
    ):
        self.tools = tools or []
        self.calls = calls or {}
        self.error_names = error_names or set()
        self.initialized = False

    def handle(self, frame: str) -> str | None:
        msg = json.loads(frame)
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "fake", "version": "9.9"},
                        "capabilities": {"tools": {}},
                    },
                }
            )
        if method == "notifications/initialized":
            self.initialized = True
            return None
        if method == "tools/list":
            return json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"tools": self.tools}})
        if method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            if name in self.calls:
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": self.calls[name]}],
                            "isError": name in self.error_names,
                        },
                    }
                )
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"content": [{"type": "text", "text": f"did-{name}"}]},
                }
            )
        return json.dumps(
            {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown method"}}
        )


RO_TOOL = {
    "name": "read_thing",
    "description": "Read a thing",
    "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    "annotations": {"readOnlyHint": True},
}
WRITE_TOOL = {
    "name": "write_thing",
    "description": "Write a thing",
    "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    "annotations": {"readOnlyHint": False},
}


def make_client(
    tools: list[dict] | None = None,
    calls: dict[str, str] | None = None,
    error_names: set[str] | None = None,
    **kwargs: Any,
) -> McpClient:
    server = FakeMcpServer(tools, calls, error_names)
    client = McpClient(InProcessTransport(server.handle), **kwargs)
    client.connect()
    return client


def make_ctx(tmp_path: Path, mcp: Any = None) -> ToolContext:
    root = tmp_path / "work"
    root.mkdir(parents=True, exist_ok=True)
    from rinari.policy.engine import PermissionProfile
    from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
    from rinari.runtime.cancellation import CancellationToken
    from rinari.shared.clock import FakeClock

    return ToolContext(
        session_id="ses-mcp",
        kind="CHAT",
        cwd=root,
        project_root=None,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=30, max_output_bytes=65536),
        artifact_root=tmp_path / "artifacts",
        clock=FakeClock(),
        cancellation=CancellationToken(),
        mcp=mcp,
    )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def test_request_frame_is_single_json_line():
    frame = request("tools/list", {})
    msg = json.loads(frame)
    assert msg["jsonrpc"] == "2.0"
    assert msg["method"] == "tools/list"
    assert isinstance(msg["id"], int)


def test_notification_has_no_id():
    msg = json.loads(notification("notifications/initialized"))
    assert "id" not in msg


def test_parse_message_roundtrip():
    frame = request("tools/call", {"name": "x"})
    message: McpMessage = parse_message(frame)  # type: ignore[assignment]
    assert message.is_response is False
    assert message.method == "tools/call"
    assert message.params["name"] == "x"


def test_parse_message_ignores_noise():
    assert parse_message("") is None
    assert parse_message("not json") is None
    assert parse_message('"just a string"') is None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def test_connect_does_initialize_handshake():
    client = make_client(tools=[RO_TOOL])
    info = client.server_info
    assert info == {"name": "fake", "version": "9.9"}
    assert client.connected


def test_list_tools_normalizes():
    client = make_client(tools=[RO_TOOL, WRITE_TOOL])
    tools = client.list_tools()
    assert [t.name for t in tools] == ["read_thing", "write_thing"]
    assert tools[0].annotations["readOnlyHint"] is True


def test_call_tool_returns_content():
    client = make_client(calls={"greet": "hello rinari"})
    result = client.call_tool("greet", {})
    assert result["content"][0]["text"] == "hello rinari"


def test_call_tool_is_error_raises():
    client = make_client(calls={"boom": "kaboom"}, error_names={"boom"})
    with pytest.raises(McpError) as excinfo:
        client.call_tool("boom", {})
    assert excinfo.value.code == "MCP_TOOL_FAILED"
    assert "kaboom" in excinfo.value.message


def test_unknown_method_maps_to_not_found():
    client = make_client()
    with pytest.raises(McpError) as excinfo:
        client._call("bogus/method", None)
    assert excinfo.value.code == "MCP_NOT_FOUND"


def test_call_before_connect_raises():
    client = McpClient(InProcessTransport(FakeMcpServer().handle))
    with pytest.raises(McpError) as excinfo:
        client.list_tools()
    assert excinfo.value.code == "MCP_NOT_CONNECTED"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


def test_tool_name_namespacing_and_sanitization():
    assert tool_name("gith_b", "do-thing.v2") == "mcp.gith_b.do-thing.v2"
    assert tool_name("weird name!", "a/b") == "mcp.weird_name_.a_b"


def test_adapter_classifies_read_only_hint():
    defs = mcp_tool_definitions("srv", [McpToolInfo.from_raw(t) for t in [RO_TOOL, WRITE_TOOL]])
    by_name = {d.name: d for d in defs}
    assert "mcp.read" in by_name["mcp.srv.read_thing"].capabilities
    assert "mcp.call" in by_name["mcp.srv.write_thing"].capabilities
    assert by_name["mcp.srv.read_thing"].idempotent is True
    assert by_name["mcp.srv.write_thing"].idempotent is False


class _FakeInvokeService:
    def __init__(self, payload):
        self.payload = payload

    def invoke(self, server, tool, arguments, project=None):
        return self.payload


def test_adapter_handler_invokes_service(tmp_path):
    defs = mcp_tool_definitions("srv", [McpToolInfo.from_raw(RO_TOOL)])
    result = defs[0].handler({"id": "1"}, make_ctx(tmp_path, _FakeInvokeService({"content": []})))
    assert result.ok is True
    assert result.data == {"content": []}


def test_adapter_handler_without_service_reports_dependency(tmp_path):
    defs = mcp_tool_definitions("srv", [McpToolInfo.from_raw(RO_TOOL)])
    result = defs[0].handler({}, make_ctx(tmp_path, None))
    assert result.ok is False
    assert result.error.code is ToolErrorCode.DEPENDENCY_ERROR


# ---------------------------------------------------------------------------
# Service: registry + trust + invocation
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_service(app_ctx):
    return build_services(app_ctx)


def _install_fake_transport(
    service: McpService, tools: list[dict] | None = None, calls: dict[str, str] | None = None
):
    server = FakeMcpServer(tools or [RO_TOOL, WRITE_TOOL], calls or {})
    service._transport_for = lambda row: InProcessTransport(server.handle)


def test_add_rejects_plain_secret_values(mcp_service):
    with pytest.raises(ValueError, match="env://"):
        mcp_service.mcp.add("s1", ["echo"], env_refs={"API_KEY": "plain-secret-value"})


def test_add_roundtrip_and_list(mcp_service):
    row = mcp_service.mcp.add("srv1", ["my-mcp-server", "--stdio"], env_refs={"K": "env://K"})
    assert row["name"] == "srv1"
    assert "K" in json.loads(row["config_json"])["env"]
    names = [r["name"] for r in mcp_service.mcp.list()]
    assert "srv1" in names


def test_disable_hides_tools(mcp_service):
    mcp_service.mcp.add("srv1", ["e"])
    mcp_service.mcp.disable("srv1")
    _install_fake_transport(mcp_service.mcp)
    row = mcp_service.mcp.show("srv1")
    assert row["enabled"] in (0, False)


def test_connect_uses_trusted_path(mcp_service):
    mcp_service.mcp.add("srv1", ["e"])
    _install_fake_transport(mcp_service.mcp)
    data = mcp_service.mcp.connect("srv1")
    assert data["connected"] is True
    assert data["serverInfo"]["name"] == "fake"


def test_project_scope_requires_trust(app_ctx, tmp_path):
    services = build_services(app_ctx)
    project = tmp_path / "proj"
    project.mkdir()
    services.mcp.add("proj-srv", ["e"], scope="project")
    _install_fake_transport(services.mcp)
    with pytest.raises(McpError) as excinfo:
        services.mcp.connect("proj-srv", project)
    assert excinfo.value.code == "MCP_NOT_CONNECTED"
    # Grant trust -> connects.
    services.trust.add(project)
    data = services.mcp.connect("proj-srv", project)
    assert data["connected"] is True


def test_tool_definitions_via_service(mcp_service):
    mcp_service.mcp.add("srv1", ["e"])
    _install_fake_transport(mcp_service.mcp)
    defs = mcp_service.mcp.tools("srv1")
    names = [d.name for d in defs]
    assert "mcp.srv1.read_thing" in names
    assert "mcp.srv1.write_thing" in names


def test_invoke_returns_normalized_payload(mcp_service):
    mcp_service.mcp.add("srv1", ["e"])
    _install_fake_transport(mcp_service.mcp, calls={"read_thing": "payload-123"})
    payload = mcp_service.mcp.invoke("srv1", "read_thing", {})
    assert payload["content"][0]["text"] == "payload-123"


def test_remove_disconnects(mcp_service):
    mcp_service.mcp.add("srv1", ["e"])
    _install_fake_transport(mcp_service.mcp)
    mcp_service.mcp.connect("srv1")
    assert mcp_service.mcp.remove("srv1") is True
    assert mcp_service.mcp.show("srv1") is None
