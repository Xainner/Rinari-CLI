"""MCP servers connect in the Engine, in parallel and in the background (connectors plan, C2).

Deterministic: in-memory servers gated by events, a fake monotonic clock for
the retry waits, a temporary RINARI_HOME (app_ctx) and no network. One test
spawns a local Python process to cover the stdio routing.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.capability_search import search_capabilities
from rinari.cli import agent_runtime
from rinari.engine_protocol.server import EngineServer
from rinari.mcp.client import McpClient
from rinari.mcp.resources import RESOURCES_LIST, RESOURCES_READ
from rinari.mcp.transport import InProcessTransport, StdioTransport, TransportError
from rinari.models.types import ModelRequest, ModelResponse, ProviderCapabilities, StopReason
from rinari.tools.definition import ToolContext, ToolErrorCode
from rinari.tools.exposure import is_lazy_tool
from rinari.tools.registry import ToolRegistry

READ_TOOL = {
    "name": "read_thing",
    "description": "Read a thing from the tracker",
    "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    "annotations": {"readOnlyHint": True},
}
NEW_TOOL = {
    "name": "close_thing",
    "description": "Close a thing in the tracker",
    "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
}


class FakeServer:
    """In-memory MCP server; `gate` holds `initialize` until it is set."""

    def __init__(
        self,
        tools: list[dict] | None = None,
        *,
        gate: threading.Event | None = None,
        resources: list[dict] | None = None,
    ) -> None:
        self.tools = list(tools if tools is not None else [READ_TOOL])
        self.gate = gate
        self.resources = resources
        self.transports: list[InProcessTransport] = []
        self.list_calls = 0
        self.call_error: TransportError | None = None

    def transport(self, _row=None) -> InProcessTransport:
        server = self

        class _Transport(InProcessTransport):
            def send(self, payload: str, timeout_s: float):
                method = json.loads(payload).get("method")
                if method == "tools/call" and server.call_error is not None:
                    raise server.call_error
                return super().send(payload, timeout_s)

        transport = _Transport(self.handle)
        self.transports.append(transport)
        return transport

    def handle(self, frame: str) -> str | None:
        message = json.loads(frame)
        method, rid = message.get("method"), message.get("id")
        if rid is None:
            return None
        if method == "initialize":
            if self.gate is not None:
                self.gate.wait(30)
            capabilities: dict = {"tools": {"listChanged": True}}
            if self.resources is not None:
                capabilities["resources"] = {}
            result: dict = {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "fake", "version": "1"},
                "capabilities": capabilities,
            }
        elif method == "tools/list":
            self.list_calls += 1
            result = {"tools": list(self.tools)}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "done"}]}
        elif method == "resources/list":
            result = {"resources": self.resources or []}
        elif method == "resources/read":
            uri = message["params"]["uri"]
            result = {
                "contents": [{"uri": uri, "mimeType": "text/plain", "text": f"body of {uri}"}]
            }
        else:
            return json.dumps(
                {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown"}}
            )
        return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})


class FailingTransport(InProcessTransport):
    """A server that cannot be reached (or whose sign-in renewal failed)."""

    def __init__(self, error: TransportError) -> None:
        super().__init__(lambda frame: None)
        self._error = error

    def start(self) -> None:
        raise self._error


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def services(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    container = build_services(app_ctx, user_home=user_home)
    yield container
    container.mcp.close()


@pytest.fixture
def gate():
    event = threading.Event()
    yield event
    event.set()  # never leave a connect thread waiting past the test


def _status(services, name: str) -> str:
    row = services.mcp.show(name) or services.mcp.show(name, "project")
    return services.mcp.status(row)["status"]


def test_building_a_turn_does_not_wait_for_a_slow_server(services, gate) -> None:
    server = FakeServer(gate=gate)
    services.mcp.add("tracker", ["fake-tracker"])
    services.mcp._transport_for = server.transport
    registry = ToolRegistry()

    started = time.monotonic()
    assert agent_runtime._mcp_tools(services, None) == []
    services.mcp.follow(registry)
    assert time.monotonic() - started < 1.0
    assert _status(services, "tracker") == "connecting"
    assert registry.get("mcp.tracker.read_thing") is None

    gate.set()
    assert wait_for(lambda: _status(services, "tracker") == "connected")
    # The late server joined the turn's registry: capability.search finds it.
    assert registry.get("mcp.tracker.read_thing") is not None
    found = [row["name"] for row in search_capabilities(registry, "read thing tracker")]
    assert "mcp.tracker.read_thing" in found
    assert services.mcp.status(services.mcp.show("tracker"))["connection"]["tools"] == 1


def test_servers_connect_in_parallel(services) -> None:
    gates = [threading.Event() for _ in range(3)]
    entered: list[str] = []

    class _Gated(FakeServer):
        def __init__(self, name: str, release: threading.Event) -> None:
            super().__init__()
            self.name, self.release = name, release

        def handle(self, frame):
            if json.loads(frame).get("method") == "initialize":
                entered.append(self.name)
                self.release.wait(10)
            return super().handle(frame)

    servers = {}
    for index, event in enumerate(gates):
        name = f"srv{index}"
        services.mcp.add(name, ["fake"])
        servers[name] = _Gated(name, event)
    services.mcp._transport_for = lambda row: servers[row["name"]].transport()
    try:
        services.mcp.start(None)
        # All three are inside `initialize` at once: nobody waits its turn.
        assert wait_for(lambda: len(entered) == 3)
    finally:
        for event in gates:
            event.set()
    assert wait_for(lambda: all(_status(services, n) == "connected" for n in servers))


def test_a_dead_server_delays_no_turn_not_even_the_second(services, gate) -> None:
    hanging = FakeServer(gate=gate)
    attempts: list[str] = []

    def transport(row):
        attempts.append(row["name"])
        if row["name"] == "down":
            return FailingTransport(
                TransportError(
                    "TRANSPORT_UNREACHABLE", "cannot connect", hint="check_url_or_network"
                )
            )
        return hanging.transport()

    services.mcp.add("down", url="http://127.0.0.1:9/mcp", transport="http")
    services.mcp.add("hang", ["fake"])
    services.mcp._transport_for = transport

    for _turn in range(2):
        started = time.monotonic()
        agent_runtime._mcp_tools(services, None)
        services.mcp.follow(ToolRegistry())
        assert time.monotonic() - started < 1.0
        assert wait_for(lambda: _status(services, "down") == "error")

    # The second turn tried neither again: the dead one waits for its retry
    # and the hanging one is still on its first attempt.
    assert sorted(attempts) == ["down", "hang"]
    view = services.mcp.status(services.mcp.show("down"))
    assert view["connection"]["hint"] == "check_url_or_network"
    assert view["connection"]["attempts"] == 1
    assert view["connection"]["retry_in_s"] is not None
    assert _status(services, "hang") == "connecting"


def test_a_failed_server_is_retried_with_a_growing_wait(services) -> None:
    clock = [100.0]
    services.mcp._monotonic = lambda: clock[0]
    healthy = FakeServer()
    calls: list[int] = []

    def transport(_row):
        calls.append(1)
        if len(calls) < 3:
            return FailingTransport(TransportError("TRANSPORT_TIMEOUT", "no answer"))
        return healthy.transport()

    services.mcp.add("flaky", ["fake"])
    services.mcp._transport_for = transport
    with pytest.raises(Exception, match="no answer"):
        services.mcp.connect("flaky")
    assert services.mcp.status(services.mcp.show("flaky"))["connection"]["retry_in_s"] == 2

    # A call inside the wait fails at once with the stored reason.
    with pytest.raises(Exception, match="retried in 2s"):
        services.mcp.invoke("flaky", "read_thing", {})
    assert len(calls) == 1

    clock[0] += 2.5  # the wait is over: the call tries again, and fails again
    with pytest.raises(Exception, match="no answer"):
        services.mcp.invoke("flaky", "read_thing", {})
    assert len(calls) == 2
    assert services.mcp.status(services.mcp.show("flaky"))["connection"]["retry_in_s"] == 4

    clock[0] += 4.5
    assert services.mcp.invoke("flaky", "read_thing", {})["content"][0]["text"] == "done"
    assert _status(services, "flaky") == "connected"


def test_the_engine_retries_a_failed_server_on_a_timer(services, monkeypatch) -> None:
    from rinari.mcp import service as service_module

    monkeypatch.setattr(service_module, "RETRY_BASE_S", 0.05)
    healthy = FakeServer()
    calls: list[int] = []

    def transport(_row):
        calls.append(1)
        if len(calls) == 1:
            return FailingTransport(TransportError("TRANSPORT_UNREACHABLE", "not yet"))
        return healthy.transport()

    services.mcp.add("late", ["fake"])
    services.mcp._transport_for = transport
    services.mcp.start_background()
    assert wait_for(lambda: _status(services, "late") == "connected")
    assert len(calls) == 2


def test_two_sessions_share_one_connection_and_tools_list(services) -> None:
    server = FakeServer()
    services.mcp.add("tracker", ["fake"])
    services.mcp._transport_for = server.transport
    first, second = ToolRegistry(), ToolRegistry()

    services.mcp.start(None)
    assert wait_for(lambda: _status(services, "tracker") == "connected")
    services.mcp.follow(first)
    services.mcp.start(None)  # the next turn, or another session
    services.mcp.follow(second)
    agent_runtime._mcp_tools(services, None)

    assert len(server.transports) == 1
    assert server.list_calls == 1  # tools/list is not repeated per turn
    assert first.get("mcp.tracker.read_thing") is not None
    assert second.get("mcp.tracker.read_thing") is not None


def test_list_changed_refreshes_the_tools_of_that_server(services) -> None:
    server = FakeServer()
    services.mcp.add("tracker", ["fake"])
    services.mcp._transport_for = server.transport
    registry = ToolRegistry()
    events: list[dict] = []
    services.mcp.on_status = events.append
    services.mcp.connect("tracker")
    services.mcp.follow(registry)

    server.tools.append(NEW_TOOL)
    server.transports[0].push(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    )
    assert wait_for(lambda: registry.get("mcp.tracker.close_thing") is not None)
    assert server.list_calls == 2
    assert wait_for(lambda: events and events[-1]["connection"]["tools"] == 2)

    server.tools.remove(READ_TOOL)
    server.transports[0].push(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    )
    assert wait_for(lambda: registry.get("mcp.tracker.read_thing") is None)
    assert registry.get("mcp.tracker.close_thing") is not None


def test_reauth_required_reaches_the_state(services) -> None:
    calls: list[int] = []
    events: list[dict] = []
    services.mcp.on_status = events.append

    def transport(_row):
        calls.append(1)
        return FailingTransport(
            TransportError(
                "TRANSPORT_AUTH",
                "the sign-in renewal failed",
                http_status=401,
                hint="reauth_required",
            )
        )

    services.mcp.add("mail", url="https://mail.example.test/mcp", transport="http")
    services.mcp._transport_for = transport
    services.mcp.start(None)
    assert wait_for(lambda: _status(services, "mail") == "reauth_required")
    view = services.mcp.status(services.mcp.show("mail"))
    assert view["connection"]["hint"] == "reauth_required"
    assert view["connection"]["retry_in_s"] is None  # waiting cannot fix it
    assert any(e["name"] == "mail" and e["status"] == "reauth_required" for e in events)

    # A call fails at once and clearly, without another attempt.
    from rinari.mcp.adapter import tool_error
    from rinari.mcp.client import McpError

    with pytest.raises(McpError) as excinfo:
        services.mcp.invoke("mail", "send", {})
    assert len(calls) == 1
    error = tool_error(excinfo.value)
    assert error.code is ToolErrorCode.AUTH_EXPIRED and "reconnect" in error.message

    # «Reconectar» tries again whatever the state.
    assert services.mcp.reconnect("mail")["status"] == "connecting"
    assert wait_for(lambda: len(calls) == 2)


def test_reauth_required_during_a_call_reaches_the_state(services) -> None:
    server = FakeServer()
    services.mcp.add("mail", ["fake"])
    services.mcp._transport_for = server.transport
    services.mcp.connect("mail")
    server.call_error = TransportError(
        "TRANSPORT_AUTH", "the sign-in renewal failed", http_status=401, hint="reauth_required"
    )
    with pytest.raises(Exception, match="renewal failed"):
        services.mcp.invoke("mail", "read_thing", {})
    assert _status(services, "mail") == "reauth_required"
    # Its tools stay findable, so the model can tell the owner what is wrong.
    assert services.mcp.available_tools()


def test_a_disabled_server_says_so_and_drops_its_tools(services) -> None:
    server = FakeServer()
    services.mcp.add("tracker", ["fake"])
    services.mcp._transport_for = server.transport
    registry = ToolRegistry()
    services.mcp.connect("tracker")
    services.mcp.follow(registry)
    assert registry.get("mcp.tracker.read_thing") is not None

    services.mcp.disable("tracker")
    assert _status(services, "tracker") == "disabled"
    assert registry.get("mcp.tracker.read_thing") is None
    services.mcp.start(None)
    assert len(server.transports) == 1


def test_resources_are_on_demand_reads_through_the_door(services, tmp_path) -> None:
    from rinari.policy.engine import PermissionProfile
    from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
    from rinari.runtime.cancellation import CancellationToken
    from rinari.shared.clock import FakeClock

    plain = FakeServer()
    docs = FakeServer(
        resources=[{"uri": "doc://welcome", "name": "Welcome", "mimeType": "text/plain"}]
    )
    services.mcp.add("plain", ["fake"])
    services.mcp.add("docs", ["fake"])
    servers = {"plain": plain, "docs": docs}
    services.mcp._transport_for = lambda row: servers[row["name"]].transport()
    registry = ToolRegistry()
    services.mcp.connect("plain")
    services.mcp.follow(registry)
    # No connected server offers resources yet: no resource tools at all.
    assert registry.get(RESOURCES_LIST) is None

    services.mcp.connect("docs")
    listing, reading = registry.get(RESOURCES_LIST), registry.get(RESOURCES_READ)
    assert listing is not None and reading is not None
    for tool in (listing, reading):
        assert tool.always_loaded is False and is_lazy_tool(tool)
        assert tool.capabilities == ("mcp.read",)
    assert reading.classify_action({"server": "docs", "uri": "doc://welcome"}).target == "docs"

    root = tmp_path / "work"
    root.mkdir()
    ctx = ToolContext(
        session_id="ses-res",
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
        mcp=services.mcp,
    )
    listed = listing.handler({}, ctx)
    assert listed.ok and listed.origin == "mcp"
    assert listed.data["servers"] == [
        {
            "server": "docs",
            "resources": [{"uri": "doc://welcome", "name": "Welcome", "mimeType": "text/plain"}],
        }
    ]
    read = reading.handler({"server": "docs", "uri": "doc://welcome"}, ctx)
    assert read.ok and read.data["contents"][0]["text"] == "body of doc://welcome"
    refused = reading.handler({"server": "plain", "uri": "doc://x"}, ctx)
    assert not refused.ok and refused.error.code is ToolErrorCode.NOT_FOUND


_STDIO_SERVER = r"""
import json, sys, threading, time
lock = threading.Lock()
tools = [{"name": "fast", "inputSchema": {"type": "object"}},
         {"name": "slow", "inputSchema": {"type": "object"}}]

def send(obj):
    with lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

def answer(msg):
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": True}}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        name = msg["params"]["name"]
        if name == "slow":
            time.sleep(0.4)
        if name == "grow":
            tools.append({"name": "grown", "inputSchema": {"type": "object"}})
            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        result = {"content": [{"type": "text", "text": "answer-" + name}]}
    else:
        result = {}
    send({"jsonrpc": "2.0", "id": rid, "result": result})

for line in sys.stdin:
    msg = json.loads(line)
    if "id" in msg and "method" in msg:
        threading.Thread(target=answer, args=(msg,)).start()
"""


def test_stdio_answers_by_id_and_hands_notifications_over(tmp_path: Path) -> None:
    script = tmp_path / "server.py"
    script.write_text(_STDIO_SERVER, encoding="utf-8")
    client = McpClient(StdioTransport([sys.executable, str(script)]), timeout_s=10)
    notices: list[str] = []
    client.add_listener(lambda method, _params: notices.append(method))
    client.connect()
    try:
        results: dict[str, str] = {}

        def call(name: str) -> None:
            results[name] = client.call_tool(name)["content"][0]["text"]

        # The slow answer comes second: each caller still gets its own.
        slow = threading.Thread(target=call, args=("slow",))
        slow.start()
        time.sleep(0.05)
        call("fast")
        slow.join(5)
        assert results == {"slow": "answer-slow", "fast": "answer-fast"}

        client.call_tool("grow")
        assert wait_for(lambda: "notifications/tools/list_changed" in notices)
        assert [tool.name for tool in client.list_tools()][-1] == "grown"
    finally:
        client.close()


# -- the Engine ---------------------------------------------------------------


@pytest.fixture
def engine_services(services):
    services.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    services.models.add("fake", "fake-model-1", "fake-one")
    services.providers.use("fake")
    return services


@pytest.fixture
def server(engine_services, tmp_path):
    engine = EngineServer(engine_services, user_home=tmp_path / "home")
    yield engine
    engine.close()


class FakeModel:
    def __init__(self, scripted: list[ModelResponse]) -> None:
        self.scripted = scripted
        self.requests: list[ModelRequest] = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.scripted.pop(0)


def _req(request_id, method, params=None):
    return json.dumps({"id": request_id, "method": method, "params": params or {}})


def _collect_until(server, session_id, timeout=20.0):
    seen: list = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in server.drain_events():
            if evt.get("payload", {}).get("session_id") not in (session_id, None):
                continue
            seen.append(evt)
            if evt.get("event") in {"turn.completed", "turn.cancelled", "turn.failed"}:
                return seen
        time.sleep(0.02)
    return seen


def _turn(server, session_id, tag) -> list:
    started = server.handle_line(
        _req(tag, "session.turn.start", {"session_id": session_id, "message": "hola"})
    )
    assert started is not None and started["ok"] is True, started
    events = _collect_until(server, session_id)
    assert wait_for(lambda: not server.has_active_turns(), timeout=10)
    return events


def test_the_first_turn_reaches_the_model_while_a_server_still_connects(
    server, engine_services, gate, tmp_path, monkeypatch
) -> None:
    slow = FakeServer(gate=gate)
    engine_services.mcp.add("tracker", ["fake"])
    engine_services.mcp._transport_for = slow.transport
    answer = ModelResponse(content="hola", stop_reason=StopReason.END_TURN)
    fake = FakeModel([answer, answer])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    created = server.handle_line(_req("c", "session.create", {"cwd": str(tmp_path), "chat": True}))
    session_id = created["result"]["session"]["id"]

    started = time.monotonic()
    first = _turn(server, session_id, "t1")
    names = [evt["event"] for evt in first]
    assert "model.started" in names and names[-1] == "turn.completed"
    assert time.monotonic() - started < 15  # the gate is still closed
    assert _status(engine_services, "tracker") == "connecting"

    gate.set()
    assert wait_for(lambda: _status(engine_services, "tracker") == "connected")
    _turn(server, session_id, "t2")
    one, two = fake.requests
    # F42: the late server changed neither the tool list nor its order...
    assert [t.name for t in one.tools] == [t.name for t in two.tools]
    assert not any(t.name.startswith("mcp.") for t in two.tools)
    # ...and the turn note now names what connected.
    text = lambda request: "\n".join(m.content or "" for m in request.messages)  # noqa: E731
    assert "mcp.tracker.read_thing" not in text(one)
    assert "mcp.tracker.read_thing" in text(two)


def test_mcp_views_carry_the_state_and_reconnect_emits_it(server, engine_services) -> None:
    calls: list[int] = []

    def transport(_row):
        calls.append(1)
        return FailingTransport(
            TransportError("TRANSPORT_UNREACHABLE", "cannot connect", hint="check_url_or_network")
        )

    engine_services.mcp.add("down", url="http://127.0.0.1:9/mcp", transport="http")
    engine_services.mcp._transport_for = transport
    listed = server.handle_line(_req("l1", "mcp.list"))["result"]["servers"]
    assert listed[0]["status"] == "idle" and listed[0]["connected"] is False

    reconnect = server.handle_line(_req("r1", "mcp.reconnect", {"name": "down"}))
    assert reconnect["ok"] is True
    assert reconnect["result"]["server"]["status"] in {"connecting", "error"}
    assert wait_for(lambda: _status(engine_services, "down") == "error")
    got = server.handle_line(_req("g1", "mcp.get", {"name": "down"}))["result"]["server"]
    assert got["status"] == "error"
    assert got["connection"]["hint"] == "check_url_or_network"
    assert got["connection"]["code"] == "MCP_UNREACHABLE"

    events = [e for e in server.drain_events() if e.get("event") == "mcp.status.changed"]
    assert [e["payload"]["status"] for e in events][-2:] == ["connecting", "error"]
    assert events[-1]["payload"]["name"] == "down"

    report = server.handle_line(_req("d1", "engine.diagnostics"))["result"]["diagnostics"]
    (entry,) = report["extensions"]["mcp"]
    assert entry["status"] == "error" and entry["connection"]["hint"] == "check_url_or_network"

    missing = server.handle_line(_req("r2", "mcp.reconnect", {"name": "nope"}))
    assert missing["ok"] is False and missing["error"]["code"] == "NOT_FOUND"


def test_reconnect_drops_the_old_session_and_opens_a_new_one(services) -> None:
    server = FakeServer()
    services.mcp.add("tracker", ["fake"])
    services.mcp._transport_for = server.transport
    services.mcp.connect("tracker")
    old = server.transports[0]

    assert services.mcp.reconnect("tracker")["status"] == "connecting"
    assert wait_for(lambda: _status(services, "tracker") == "connected")
    assert len(server.transports) == 2
    assert old._closed and not server.transports[1]._closed


def test_an_untrusted_project_server_keeps_its_reason_and_waits(services, tmp_path) -> None:
    from rinari.mcp.client import McpError

    project = tmp_path / "proj"
    project.mkdir()
    services.mcp.add("local-db", ["fake"], scope="project")
    services.mcp._transport_for = FakeServer().transport
    services.mcp.start(project)  # untrusted: nothing starts
    assert _status(services, "local-db") == "idle"
    with pytest.raises(McpError) as excinfo:
        services.mcp.connect("local-db", project)
    assert excinfo.value.hint == "project_not_trusted"
    assert _status(services, "local-db") == "idle"

    services.trust.add(project)
    services.mcp.start(project)
    assert wait_for(lambda: _status(services, "local-db") == "connected")
    assert services.mcp.available_tools(project)
    assert services.mcp.available_tools(None) == []
