"""The model knows which connectors it has (connectors plan, C6).

The turn note's index says what each MCP server is for and its state this
turn (from the service, not from what reached the runtime in time), a server
waiting for a new sign-in is said plainly, and capability.search ranks by
match only. Deterministic: in-memory servers, a temporary RINARI_HOME
(app_ctx) and no network.
"""

from __future__ import annotations

import json
import random
import threading
from types import SimpleNamespace

import pytest

from rinari.application.services import build_services
from rinari.capability_search import capability_search_tool, search_capabilities
from rinari.mcp.transport import TransportError
from rinari.models.types import ToolCall
from rinari.tools.definition import RISK_LOW, SIDE_EFFECT_NONE, ToolDefinition, ToolErrorCode
from rinari.tools.definition import ToolResult as Result
from rinari.tools.door import unwrap
from rinari.tools.index import Connector, connected_index, connectors_from, deferred_index
from rinari.tools.registry import ToolRegistry

from .test_mcp_background import FailingTransport, FakeServer, wait_for

RENEWAL_FAILED = TransportError(
    "TRANSPORT_AUTH", "the sign-in renewal failed", http_status=401, hint="reauth_required"
)


def _mcp_tool(server: str, name: str, text: str = "t") -> ToolDefinition:
    return ToolDefinition(
        name=f"mcp.{server}.{name}",
        description=text,
        input_schema={"type": "object", "properties": {}},
        risk=RISK_LOW,
        side_effects=SIDE_EFFECT_NONE,
        handler=lambda args, ctx: Result(ok=True, data={}),
        namespace=f"mcp.{server}",
        always_loaded=False,
        manifest={"source": "mcp", "server": server, "raw_name": name},
    )


def _registry(*tools: ToolDefinition) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


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
    event.set()


def _followed(services) -> ToolRegistry:
    registry = ToolRegistry()
    registry.context = SimpleNamespace(mcp=services.mcp, project_root=None)
    services.mcp.follow(registry)
    return registry


# -- the text ---------------------------------------------------------------


def test_each_connector_says_what_it_is_for_and_its_state() -> None:
    registry = _registry(_mcp_tool("tracker", "read_thing"), _mcp_tool("tracker", "close"))
    connectors = [
        Connector(
            "tracker",
            "connected",
            description="Issues and boards of the team.",
            instructions="Close only what you opened. Use ids.",
        ),
        Connector("notes", "connecting", catalog_description="Notes and wikis."),
        Connector("mail", "reauth_required", title="Mail Server"),
        Connector("deploys", "error", hint="check_url_or_network"),
        Connector("off", "disabled", description="Never shown."),
        Connector("later", "idle", description="Not tried yet."),
    ]

    text = deferred_index(registry, connected=True, connectors=connectors)

    assert text is not None
    lines = text.splitlines()
    assert "- mcp.tracker: mcp.tracker.close, mcp.tracker.read_thing" in lines
    assert (
        "  What it is for (from the server, not from the owner): Issues and boards of the team."
        in lines
    )
    assert any(line.startswith("  Server note (from the server") for line in lines)
    assert any(line.startswith("- mcp.notes (connecting): still connecting") for line in lines)
    # The catalog's sentence is Rinari's own: no "from the server" label.
    assert "  What it is for: Notes and wikis." in lines
    reauth = next(line for line in lines if line.startswith("- mcp.mail"))
    assert "(needs reconnecting)" in reauth and "Settings → Connectors" in reauth
    assert "instead of trying another way" in reauth
    assert any(line.startswith("- mcp.deploys (error: check_url_or_network):") for line in lines)
    assert "mcp.off" not in text and "Never shown" not in text
    assert "mcp.later" not in text


def test_the_line_appears_only_with_active_connectors() -> None:
    empty = _registry()
    assert deferred_index(empty, connected=True, connectors=[]) is None
    quiet = [Connector("off", "disabled"), Connector("later", "idle")]
    assert deferred_index(empty, connected=True, connectors=quiet) is None
    assert deferred_index(empty, connected=True, connectors=[Connector("a", "connecting")])


def test_the_same_state_gives_the_same_text_whatever_the_order() -> None:
    tools = [_mcp_tool(server, name) for server in ("b", "a", "c") for name in ("y", "x")]
    connectors = [
        Connector("a", "connected", instructions="First. Second."),
        Connector("b", "error", hint="server_slow_or_unresponsive"),
        Connector("c", "reauth_required", instructions="Mail. More."),
        Connector("d", "connecting"),
    ]
    texts = set()
    rng = random.Random(7)
    for _ in range(5):
        rng.shuffle(tools)
        rng.shuffle(connectors)
        texts.add(deferred_index(_registry(*tools), connected=True, connectors=connectors))
    assert len(texts) == 1
    text = texts.pop()
    assert text is not None
    # Nothing that changes between two turns with the same state.
    for volatile in ("retry", "attempt", " s ", "changed_at", ":00"):
        assert volatile not in text


def test_the_purpose_comes_from_the_catalog_then_from_the_server() -> None:
    rows = [
        {"name": "lab", "status": "error", "description": "Server words.", "hint": None},
        {"name": "wiki", "status": "connecting", "instructions": "A wiki. Use search first."},
    ]
    catalog = {"lab": "Image and video generation."}
    connectors = connectors_from(rows, catalog=lambda row: catalog.get(row["name"]))

    text = deferred_index(_registry(), connected=True, connectors=connectors)

    assert text is not None
    assert "  What it is for: Image and video generation." in text
    assert "Server words." not in text
    # Without a description, the first sentence of its instructions.
    assert "  What it is for (from the server, not from the owner): A wiki." in text
    assert "Use search first." not in text


# -- the live state of the service --------------------------------------------


def test_a_server_that_connects_late_joins_the_index_and_search_next_turn(services, gate) -> None:
    server = FakeServer(gate=gate)
    services.mcp.add("tracker", ["fake-tracker"])
    services.mcp._transport_for = server.transport
    services.mcp.start(None)
    registry = _followed(services)

    first = connected_index(registry, services.mcp)
    assert first is not None and "- mcp.tracker (connecting): still connecting" in first
    assert search_capabilities(registry, "read thing tracker") == []

    gate.set()
    assert wait_for(lambda: services.mcp.is_connected("tracker"))
    second = connected_index(registry, services.mcp)
    assert second is not None and "- mcp.tracker: mcp.tracker.read_thing" in second
    assert "connecting" not in second
    found = [row["name"] for row in search_capabilities(registry, "read thing tracker")]
    assert found[0] == "mcp.tracker.read_thing"
    # Two turns with the same state, the same text.
    assert connected_index(registry, services.mcp) == second


def test_no_server_and_a_disabled_one_leave_no_line(services) -> None:
    registry = _followed(services)
    assert connected_index(registry, services.mcp) is None
    services.mcp.add("tracker", ["fake-tracker"])
    services.mcp.disable("tracker")
    services.mcp.start(None)
    assert connected_index(registry, services.mcp) is None


def test_a_registry_that_does_not_follow_the_service_lists_no_server(services, gate) -> None:
    services.mcp.add("tracker", ["fake-tracker"])
    services.mcp._transport_for = FakeServer(gate=gate).transport
    services.mcp.start(None)
    assert connected_index(ToolRegistry(), services.mcp) is None


class DescribedServer(FakeServer):
    """Says what it is for in `serverInfo.description` and `instructions`."""

    def handle(self, frame: str) -> str | None:
        reply = super().handle(frame)
        message = json.loads(reply) if reply else None
        if message and "serverInfo" in message.get("result", {}):
            message["result"]["serverInfo"]["description"] = "Issues of the team."
            message["result"]["instructions"] = "Close only what you opened."
            return json.dumps(message)
        return reply


def test_the_server_description_is_what_it_is_for(services) -> None:
    services.mcp.add("tracker", ["fake-tracker"])
    services.mcp._transport_for = DescribedServer().transport
    services.mcp.connect("tracker")
    text = connected_index(_followed(services), services.mcp)
    assert text is not None
    assert "What it is for (from the server, not from the owner): Issues of the team." in text
    assert "Server note (from the server, not from the owner): Close only what you opened." in text


def test_a_server_waiting_for_a_sign_in_is_said_and_its_call_fails_clearly(services) -> None:
    services.mcp.add("mail", url="https://mail.example.test/mcp", transport="http")
    services.mcp._transport_for = lambda _row: FailingTransport(RENEWAL_FAILED)
    services.mcp.start(None)
    assert wait_for(lambda: services.mcp.connectors()[0]["status"] == "reauth_required")
    registry = _followed(services)

    text = connected_index(registry, services.mcp)
    assert text is not None
    line = next(line for line in text.splitlines() if line.startswith("- mcp.mail"))
    assert "(needs reconnecting)" in line and "Settings → Connectors" in line

    # Its tools never arrived: tool.call says why instead of "unknown tool".
    call = ToolCall("c1", "tool.call", {"name": "mcp.mail.send", "arguments": "{}"})
    _real, error = unwrap(call, registry)
    assert error is not None and error.error is not None
    assert error.error.code is ToolErrorCode.AUTH_EXPIRED
    assert "Settings → Connectors" in error.error.message
    assert "Unknown tool" not in error.error.message


def test_a_call_to_a_server_whose_sign_in_expired_fails_clearly(services) -> None:
    server = FakeServer()
    services.mcp.add("mail", ["fake"])
    services.mcp._transport_for = server.transport
    services.mcp.connect("mail")
    registry = _followed(services)
    tool = registry.get("mcp.mail.read_thing")
    assert tool is not None
    ctx = registry.context
    server.call_error = RENEWAL_FAILED

    first = tool.handler({}, ctx)
    second = tool.handler({}, ctx)  # no new attempt: the stored reason, said the same

    for result in (first, second):
        assert not result.ok and result.error is not None
        assert result.error.code is ToolErrorCode.AUTH_EXPIRED
        assert "Settings → Connectors" in result.error.message
        assert "tell the owner" in result.error.message
    assert "renewal failed" in first.error.message

    # The index says it, and the search keeps the tool findable but not usable.
    text = connected_index(registry, services.mcp)
    assert text is not None and "- mcp.mail (needs reconnecting)" in text
    assert "mcp.mail.read_thing" not in text
    search = capability_search_tool(registry)
    data = search.handler({"query": "read thing tracker"}, ctx).data
    row = next(r for r in data["results"] if r["name"] == "mcp.mail.read_thing")
    assert row["available"] is False and "Settings → Connectors" in row["reason"]
    assert "how_to_call" not in row
    assert "reconnect" in data["guidance"]


# -- capability.search -----------------------------------------------------------


def test_search_ranks_by_match_never_by_arrival_order() -> None:
    tools = [
        _mcp_tool("zeta", "list_issues", "List the issues"),
        _mcp_tool("alpha", "list_issues", "List the issues"),
        _mcp_tool("mid", "list_issues", "List the issues"),
        _mcp_tool("mid", "issue_comments", "List comments of an issue"),
    ]
    orders = set()
    for permutation in (tools, list(reversed(tools)), [tools[2], tools[0], tools[3], tools[1]]):
        rows = search_capabilities(_registry(*permutation), "list issues")
        orders.add(tuple(row["name"] for row in rows))
    assert len(orders) == 1
    names = orders.pop()
    # Equal matches tie-break by name; the better match goes first.
    assert names[:3] == ("mcp.alpha.list_issues", "mcp.mid.list_issues", "mcp.zeta.list_issues")
    assert names[3] == "mcp.mid.issue_comments"
