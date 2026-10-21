"""Permissions per connector and per tool (plan de conectores, C5).

Deterministic: in-process MCP servers, a temporary RINARI_HOME (`app_ctx`),
scripted approval answers, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rinari.application.services import build_services
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.mcp_tools import McpToolMethods
from rinari.mcp.adapter import facts_of, mcp_tool_definitions
from rinari.mcp.client import McpToolInfo
from rinari.mcp.transport import InProcessTransport
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import (
    CHOICES_LASTING,
    CHOICES_SESSION,
    PermissionProfile,
    PolicyAction,
    PolicyEngine,
    SessionScope,
)
from rinari.policy.mcp_tools import McpToolFacts, effective_annotations, facts_for
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.shared.errors import NotFoundError
from rinari.tools.definition import ToolContext, ToolErrorCode, TurnState
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime

READ = PermissionProfile.READ_ONLY
WORK = PermissionProfile.WORKSPACE
FULL = PermissionProfile.FULL_ACCESS
ALLOW, ASK, DENY = PolicyAction.ALLOW, PolicyAction.ASK, PolicyAction.DENY

ANNOTATIONS = {
    "none": {},
    "read": {"readOnlyHint": True},
    "destructive": {"readOnlyHint": False, "destructiveHint": True},
    "closed": {"openWorldHint": False},
    # destructiveHint means nothing on a read (MCP spec).
    "read+destructive": {"readOnlyHint": True, "destructiveHint": True},
}


def _scope(profile, **kwargs) -> SessionScope:
    return SessionScope(kind="CHAT", root=None, cwd=Path("."), profile=profile, **kwargs)


def _facts(annotation: str, setting: str | None = None, server: str = "alpha") -> McpToolFacts:
    return facts_for(
        server,
        "do_it",
        effective_annotations(ANNOTATIONS[annotation]),
        decision=setting,
        decision_source="tool",
    )


def _decide(profile, facts: McpToolFacts, **scope_kwargs):
    capability = "mcp.read" if facts.read_only else "mcp.call"
    return PolicyEngine().decide(capability, _scope(profile, **scope_kwargs), mcp=facts)


# -- mode by annotation by tool setting ---------------------------------------------

TABLE = [
    # annotation, tool setting, read-only, workspace, full-access
    ("none", None, DENY, ALLOW, ALLOW),
    ("none", "allow", DENY, ALLOW, ALLOW),
    ("none", "ask", DENY, ASK, ALLOW),
    ("none", "deny", DENY, DENY, DENY),
    ("read", None, DENY, ALLOW, ALLOW),
    ("read", "allow", DENY, ALLOW, ALLOW),
    ("read", "ask", DENY, ASK, ALLOW),
    ("read", "deny", DENY, DENY, DENY),
    ("destructive", None, DENY, ASK, ALLOW),
    ("destructive", "allow", DENY, ALLOW, ALLOW),
    ("destructive", "ask", DENY, ASK, ALLOW),
    ("destructive", "deny", DENY, DENY, DENY),
    ("closed", None, DENY, ALLOW, ALLOW),
    ("closed", "deny", DENY, DENY, DENY),
    ("read+destructive", None, DENY, ALLOW, ALLOW),
]


@pytest.mark.parametrize(("annotation", "setting", "read_only", "workspace", "full"), TABLE)
def test_mode_annotation_and_tool_setting_table(annotation, setting, read_only, workspace, full):
    facts = _facts(annotation, setting)
    assert _decide(READ, facts).action is read_only
    assert _decide(WORK, facts).action is workspace
    assert _decide(FULL, facts).action is full


def test_block_wins_in_every_mode_and_says_why() -> None:
    for profile in PermissionProfile:
        decision = _decide(profile, _facts("read", "deny"))
        assert decision.action is DENY
        assert decision.rule_id == "mcp_tool_blocked"
        assert "blocked" in decision.reason


def test_asks_from_the_setting_or_a_destructive_hint_offer_always() -> None:
    asked = _decide(WORK, _facts("none", "ask"))
    assert asked.rule_id == "mcp_tool_ask"
    assert asked.choices == CHOICES_LASTING and asked.binding_mode == "exact"
    assert asked.target == "alpha/do_it"
    destructive = _decide(WORK, _facts("destructive"))
    assert destructive.rule_id == "mcp_destructive"
    assert destructive.risk == "high" and destructive.risk_class == "remote-destructive"
    assert destructive.choices == CHOICES_LASTING


def test_full_access_asks_nothing_but_blocks_what_the_owner_blocked() -> None:
    external = {"external_content": True, "external_origins": frozenset({"example.com"})}
    for annotation in ANNOTATIONS:
        for setting in (None, "allow", "ask"):
            facts = _facts(annotation, setting)
            assert _decide(FULL, facts, **external).action is ALLOW
            assert _decide(FULL, facts, ask_new_destinations=True, **external).action is ALLOW
        assert _decide(FULL, _facts(annotation, "deny"), **external).action is DENY


def test_owner_corrections_replace_the_server_hints() -> None:
    # «This one is a read».
    hints = effective_annotations({"readOnlyHint": False}, {"readOnlyHint": True})
    assert facts_for("a", "t", hints).read_only is True
    # «This one is destructive» also makes a declared read a write.
    hints = effective_annotations({"readOnlyHint": True}, {"destructiveHint": True})
    facts = facts_for("a", "t", hints)
    assert facts.read_only is False and facts.destructive is True
    # A correction of one key leaves the server's other hints.
    hints = effective_annotations({"openWorldHint": False}, {"destructiveHint": False})
    assert hints == {"readOnlyHint": None, "destructiveHint": False, "openWorldHint": False}


def test_without_facts_mcp_calls_keep_the_v4_behavior() -> None:
    engine = PolicyEngine()
    assert engine.decide("mcp.call", _scope(WORK)).action is ALLOW
    assert engine.decide("mcp.call", _scope(READ)).action is DENY
    assert engine.decide("mcp.read", _scope(WORK, external_content=True)).action is ALLOW


# -- the guard of external content: one connector and two ------------------------------


def test_same_connector_is_the_same_destination() -> None:
    from_alpha = {"external_content": True, "external_origins": frozenset({"mcp:alpha"})}
    for annotation in ("none", "closed"):
        assert _decide(WORK, _facts(annotation), **from_alpha).action is ALLOW
    # Its own setting still applies, with «always».
    assert _decide(WORK, _facts("none", "ask"), **from_alpha).choices == CHOICES_LASTING


def test_passing_data_to_another_connector_asks_for_this_session_at_most() -> None:
    from_beta = {"external_content": True, "external_origins": frozenset({"mcp:beta"})}
    decision = _decide(WORK, _facts("none"), **from_beta)
    assert decision.action is ASK
    assert decision.rule_id == "external_content_send"
    assert decision.choices == CHOICES_SESSION and decision.binding_mode == "exact"
    # Even when the owner allowed the tool: the guard is not the mode's rule.
    assert _decide(WORK, _facts("none", "allow"), **from_beta).action is ASK
    # A destructive tool asks once, still without «always».
    destructive = _decide(WORK, _facts("destructive"), **from_beta)
    assert destructive.rule_id == "external_content_send"
    assert destructive.choices == CHOICES_SESSION
    assert "destructive" in destructive.reason


def test_mixed_or_unknown_origins_count_as_another_source() -> None:
    mixed = frozenset({"mcp:alpha", "news.example.com"})
    decision = _decide(WORK, _facts("none"), external_content=True, external_origins=mixed)
    assert decision.action is ASK
    # Only the flag (an older caller): foreign to every connector.
    assert _decide(WORK, _facts("none"), external_content=True).action is ASK


def test_reads_and_closed_world_tools_do_not_send_out() -> None:
    from_beta = {"external_content": True, "external_origins": frozenset({"mcp:beta"})}
    assert _decide(WORK, _facts("read"), **from_beta).action is ALLOW
    assert _decide(WORK, _facts("closed"), **from_beta).action is ALLOW


def test_a_connector_result_still_guards_sending_to_the_internet() -> None:
    scope = _scope(WORK, external_content=True, external_origins=frozenset({"mcp:alpha"}))
    decision = PolicyEngine().decide(
        "network.outbound", scope, host="api.example.com", network_mode="send"
    )
    assert decision.action is ASK and decision.rule_id == "external_content_send"


# -- end to end through the tool runtime ---------------------------------------------------


class _FakeMcp:
    """What the handlers and «Siempre» need from McpService."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.saved: list[tuple[str, str, dict]] = []

    def invoke(self, server, tool, arguments, project=None):
        self.calls.append((server, tool))
        return {"content": [{"type": "text", "text": f"{server}:{tool}"}]}

    def set_tool_permission(self, server, tool, **changes):
        self.saved.append((server, tool, changes))


def _tools(server: str, permissions: dict | None = None):
    infos = [
        McpToolInfo.from_raw(
            {
                "name": "search",
                "description": "Search",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            }
        ),
        McpToolInfo.from_raw(
            {
                "name": "send",
                "description": "Send a message",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": False, "openWorldHint": True},
            }
        ),
    ]
    return mcp_tool_definitions(server, infos, permissions)


def _runtime(tmp_path: Path, answers: list[str], *, kind: str = "PROJECT", permissions=None):
    registry = ToolRegistry()
    for tool in [*_tools("alpha", permissions), *_tools("beta")]:
        registry.register(tool)
    asked: list = []

    def prompt(request):
        asked.append(request)
        return answers.pop(0) if answers else "n"

    runtime = ToolRuntime(registry, PolicyEngine(), ApprovalEngine(prompt=prompt))
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    service = _FakeMcp()
    ctx = ToolContext(
        session_id="s1",
        kind=kind,
        cwd=root,
        project_root=root if kind == "PROJECT" else None,
        user_home=tmp_path,
        profile=WORK,
        sandbox=FilesystemSandbox(root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=5.0),
        artifact_root=tmp_path / "artifacts",
        clock=FakeClock(start=1_700_000_000.0, step=0.1),
        cancellation=CancellationToken(),
        turn_state=TurnState(),
        mcp=service,
    )
    return runtime, ctx, asked, service


def test_reading_a_connector_and_calling_it_again_asks_nothing(tmp_path) -> None:
    runtime, ctx, asked, service = _runtime(tmp_path, [])
    assert runtime.execute("mcp.alpha.search", {}, ctx).ok
    assert ctx.turn_state.origins == {"mcp:alpha"}
    assert runtime.execute("mcp.alpha.send", {}, ctx).ok
    assert asked == []
    assert service.calls == [("alpha", "search"), ("alpha", "send")]


def test_reading_one_connector_and_sending_with_another_asks(tmp_path) -> None:
    runtime, ctx, asked, service = _runtime(tmp_path, ["n"])
    assert runtime.execute("mcp.alpha.search", {}, ctx).ok
    result = runtime.execute("mcp.beta.send", {}, ctx)
    assert result.ok is False and result.error.code is ToolErrorCode.APPROVAL_DENIED
    [request] = asked
    assert request.rule_id == "external_content_send"
    assert request.target == "beta/send"
    assert "allow_project" not in request.choices
    assert ("beta", "send") not in service.calls


def test_always_on_a_tool_card_saves_allow_for_that_tool(tmp_path) -> None:
    permissions = {"tools": {"send": {"decision": "ask"}}}
    runtime, ctx, asked, service = _runtime(tmp_path, ["p"], kind="CHAT", permissions=permissions)
    assert runtime.execute("mcp.alpha.send", {}, ctx).ok
    [request] = asked
    assert request.rule_id == "mcp_tool_ask"
    # A loose chat keeps «Siempre» here: the setting belongs to the server.
    assert "allow_project" in request.choices
    assert service.saved == [("alpha", "send", {"decision": "allow"})]
    assert runtime.approvals.grants() == []


def test_blocked_tool_is_refused_before_running(tmp_path) -> None:
    permissions = {"default": "deny", "tools": {"search": {"decision": "allow"}}}
    runtime, ctx, asked, service = _runtime(tmp_path, [], permissions=permissions)
    assert runtime.execute("mcp.alpha.search", {}, ctx).ok
    result = runtime.execute("mcp.alpha.send", {}, ctx)
    assert result.error.code is ToolErrorCode.POLICY_DENIED
    assert "server" in result.error.message
    assert asked == [] and service.calls == [("alpha", "search")]


# -- the service keeps the settings with the server ------------------------------------------

TOOLS = [
    {
        "name": "list_mail",
        "description": "List messages",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "delete_mail",
        "description": "Delete a message",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    },
    {
        "name": "plain",
        "description": "No annotations",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class _Server:
    def __init__(self, tools: list[dict]) -> None:
        self.tools = tools

    def handle(self, frame: str) -> str | None:
        msg = json.loads(frame)
        method, rid = msg.get("method"), msg.get("id")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "fake", "version": "1"},
                "capabilities": {"tools": {}},
            }
        elif method == "tools/list":
            result = {"tools": self.tools}
        else:
            result = {"content": [{"type": "text", "text": "ok"}]}
        return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})


@pytest.fixture
def services(app_ctx):
    services = build_services(app_ctx)
    server = _Server(TOOLS)
    services.mcp._transport_for = lambda row: InProcessTransport(server.handle)
    services.mcp.add("mail", ["fake-mail-server"])
    services.mcp.connect("mail")
    yield services
    services.mcp.close()


def _facts_by_tool(services) -> dict[str, McpToolFacts]:
    return {tool.manifest["raw_name"]: facts_of(tool) for tool in services.mcp.known_tools("mail")}


def test_settings_reclassify_the_live_tools_without_reconnecting(services) -> None:
    client = services.mcp.client("mail")
    services.mcp.set_tool_permission("mail", "plain", decision="deny")
    services.mcp.set_tool_permission("mail", "list_mail", annotations={"destructiveHint": True})
    facts = _facts_by_tool(services)
    assert facts["plain"].decision == "deny" and facts["plain"].decision_source == "tool"
    assert facts["list_mail"].read_only is False and facts["list_mail"].destructive is True
    by_name = {tool.manifest["raw_name"]: tool for tool in services.mcp.known_tools("mail")}
    assert by_name["list_mail"].capabilities == ("mcp.call",)
    assert by_name["list_mail"].idempotent is False
    assert services.mcp.client("mail") is client  # same connection


def test_server_default_applies_to_tools_without_their_own_setting(services) -> None:
    services.mcp.set_default_permission("mail", "ask")
    services.mcp.set_tool_permission("mail", "list_mail", decision="allow")
    facts = _facts_by_tool(services)
    assert (facts["plain"].decision, facts["plain"].decision_source) == ("ask", "server")
    assert (facts["list_mail"].decision, facts["list_mail"].decision_source) == ("allow", "tool")
    services.mcp.set_default_permission("mail", None)
    assert _facts_by_tool(services)["plain"].decision is None


def test_settings_survive_editing_the_server_and_hold_no_secret(services) -> None:
    services.mcp.set_tool_permission("mail", "delete_mail", decision="deny")
    services.mcp.update("mail", {"timeout_s": 12})
    stored = json.loads(services.mcp.show("mail")["config_json"])
    assert stored["permissions"] == {"tools": {"delete_mail": {"decision": "deny"}}}
    assert stored["timeout_s"] == 12
    # Clearing the last setting removes the block.
    services.mcp.set_tool_permission("mail", "delete_mail", decision=None)
    assert "permissions" not in json.loads(services.mcp.show("mail")["config_json"])


def test_a_project_server_keeps_its_folder_when_its_permissions_change(services, tmp_path) -> None:
    folder = tmp_path / "project-a"
    folder.mkdir()
    services.mcp.add("local-db", ["fake-db"], scope="project", project_root=folder)
    root = services.mcp.show("local-db", "project")["project_root"]
    assert root
    services.mcp.set_tool_permission("local-db", "plain", decision="allow", scope="project")
    services.mcp.set_default_permission("local-db", "ask", scope="project")
    # Still tied to its folder: never visible in every trusted project.
    assert services.mcp.show("local-db", "project")["project_root"] == root


def test_invalid_settings_are_refused(services) -> None:
    with pytest.raises(ValueError, match="allow, ask, deny"):
        services.mcp.set_tool_permission("mail", "plain", decision="maybe")
    with pytest.raises(ValueError, match="only corrects"):
        services.mcp.set_tool_permission("mail", "plain", annotations={"idempotentHint": True})
    with pytest.raises(ValueError, match="true, false or null"):
        services.mcp.set_tool_permission("mail", "plain", annotations={"readOnlyHint": "yes"})
    with pytest.raises(LookupError):
        services.mcp.set_default_permission("nobody", "ask")


# -- mcp.tools.list / set / set_default -------------------------------------------------------


def test_tools_list_shows_hints_setting_source_and_modes(services) -> None:
    methods = McpToolMethods(services)
    server = methods.list({"name": "mail"})["server"]
    assert server["name"] == "mail" and server["status"] == "connected"
    assert server["default"] is None
    tools = {tool["name"]: tool for tool in server["tools"]}
    assert set(tools) == {"list_mail", "delete_mail", "plain"}
    delete = tools["delete_mail"]
    assert delete["tool"] == "mcp.mail.delete_mail"
    assert delete["description"] == "Delete a message"
    assert delete["kind"] == "destructive" and delete["sends_out"] is False
    assert delete["annotations"]["server"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": False,
    }
    assert delete["decision"] is None and delete["decision_source"] == "mode"
    assert delete["by_mode"] == {"read-only": "deny", "workspace": "ask", "full-access": "allow"}
    assert tools["list_mail"]["kind"] == "read"
    assert tools["plain"]["sends_out"] is True


def test_tools_set_and_set_default_change_what_applies(services) -> None:
    methods = McpToolMethods(services)
    result = methods.set({"name": "mail", "tool": "plain", "decision": "deny"})
    assert result["tool"]["setting"] == "deny" and result["tool"]["decision_source"] == "tool"
    assert set(result["tool"]["by_mode"].values()) == {"deny"}
    result = methods.set(
        {"name": "mail", "tool": "delete_mail", "annotations": {"destructiveHint": False}}
    )
    assert result["tool"]["annotations"]["owner"] == {"destructiveHint": False}
    assert result["tool"]["by_mode"]["workspace"] == "allow"
    server = methods.set_default({"name": "mail", "decision": "ask"})["server"]
    assert server["default"] == "ask"
    tools = {tool["name"]: tool for tool in server["tools"]}
    assert tools["list_mail"]["decision_source"] == "server"
    assert tools["list_mail"]["by_mode"]["workspace"] == "ask"
    assert tools["plain"]["decision_source"] == "tool"


def test_a_setting_for_a_tool_the_server_no_longer_lists_is_shown(services) -> None:
    methods = McpToolMethods(services)
    methods.set({"name": "mail", "tool": "old_tool", "decision": "deny"})
    tools = {tool["name"]: tool for tool in methods.list({"name": "mail"})["server"]["tools"]}
    assert tools["old_tool"]["listed"] is False and tools["old_tool"]["setting"] == "deny"


def test_tools_methods_validate_their_params(services) -> None:
    methods = McpToolMethods(services)
    with pytest.raises(NotFoundError):
        methods.list({"name": "nobody"})
    with pytest.raises(EngineProtocolError):
        methods.set({"name": "mail", "tool": "plain"})
    with pytest.raises(EngineProtocolError):
        methods.set({"name": "mail", "tool": "plain", "decision": "always"})
    with pytest.raises(EngineProtocolError):
        methods.set_default({"name": "mail"})
    with pytest.raises(EngineProtocolError):
        methods.list({"name": "mail", "scope": "everywhere"})
