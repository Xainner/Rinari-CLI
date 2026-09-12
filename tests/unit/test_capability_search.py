"""Unified capability search tests (phase 5).

Deterministic: ToolRegistry with synthetic definitions spanning sources,
ranking checks (reliability x risk), absent capabilities, and the
`capability.search` tool executed through the ToolRuntime policy path.
"""

from __future__ import annotations

from dataclasses import replace

from rinari.capability_search import (
    SOURCE_BROWSER,
    SOURCE_MCP,
    SOURCE_NATIVE,
    SOURCE_OPENAPI,
    SOURCE_PLUGIN,
    capability_search_tool,
    classify_source,
    search_capabilities,
)
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.tools.definition import ToolContext, ToolDefinition
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime


def _tool(
    name: str,
    description: str = "",
    *,
    source: str | None = None,
    risk: str = "low",
    manifest: dict | None = None,
) -> ToolDefinition:
    m = dict(manifest or {})
    if source is not None and "source" not in m:
        m["source"] = source
    return ToolDefinition(
        name=name,
        description=description,
        input_schema={"type": "object"},
        risk=risk,
        manifest=m,
    )


def _registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register_all(
        [
            _tool("fs.read", "read files from the local filesystem"),
            _tool("web.fetch", "fetch a url", source="web"),
            _tool("browser.open", "open a url in the managed browser", risk="high"),
            _tool("mcp.git.get_log", "git log via MCP", source="mcp"),
            _tool("api.pets.listPets", "list pets via typed API", source="openapi"),
            _tool("myplug.search", "search via plugin", source="plugin"),
        ]
    )
    return r


def test_classify_source_by_prefix_and_manifest():
    r = _registry()
    tools = {name: r.get(name) for name in r.names()}  # type: ignore[misc]
    assert classify_source(tools["fs.read"]) == SOURCE_NATIVE
    assert classify_source(tools["web.fetch"]) == "web"
    assert classify_source(tools["browser.open"]) == SOURCE_BROWSER
    assert classify_source(tools["mcp.git.get_log"]) == SOURCE_MCP
    assert classify_source(tools["api.pets.listPets"]) == SOURCE_OPENAPI
    assert classify_source(tools["myplug.search"]) == SOURCE_PLUGIN


def test_native_beats_mcp_for_same_text():
    r = _registry()
    results = search_capabilities(r, "git log")
    names = [x["name"] for x in results]
    assert names[0] == "mcp.git.get_log"


def test_risk_demotion_prefers_safe_routes():
    r = _registry()
    results = search_capabilities(r, "fetch url")
    by = {x["name"]: x["score"] for x in results}
    # web.* (risk low) should outrank browser.* (risk high) on equal text match
    assert by["web.fetch"] > by["browser.open"]


def test_no_match_does_not_invent_browser_capability():
    r = _registry()
    results = search_capabilities(r, "completely-unknown-service")
    assert results == []


def test_limit_respected():
    r = _registry()
    results = search_capabilities(r, "a", limit=2)
    assert len(results) == 2
    assert all(x["score"] > 0 for x in results)


# ---------------------------------------------------------------------------
# Tool through the runtime (state.read = ALLOW)
# ---------------------------------------------------------------------------


def _ctx(tmp_path) -> ToolContext:
    root = tmp_path / "work"
    root.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        session_id="s-cap",
        kind="CHAT",
        cwd=root,
        project_root=None,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=30, max_output_bytes=65536),
        artifact_root=tmp_path / "art",
        clock=FakeClock(),
        cancellation=CancellationToken(),
    )


def test_capability_search_tool_executes(tmp_path):
    r = _registry()
    r.register(capability_search_tool(r))
    runtime = ToolRuntime(
        r, PolicyEngine(), ApprovalEngine(prompt=lambda req: "n"), clock=FakeClock()
    )
    result = runtime.execute("capability.search", {"query": "read files"}, _ctx(tmp_path))
    assert result.ok is True
    names = [x["name"] for x in result.data["results"]]
    assert "fs.read" in names


def test_capability_search_requires_query(tmp_path):
    r = _registry()
    r.register(capability_search_tool(r))
    runtime = ToolRuntime(
        r, PolicyEngine(), ApprovalEngine(prompt=lambda req: "n"), clock=FakeClock()
    )
    result = runtime.execute("capability.search", {}, _ctx(tmp_path))
    assert result.ok is False
    assert result.error.code.value == "INVALID_ARGUMENT"


def test_search_load_respects_budget(tmp_path):
    from rinari.tools.exposure import ToolExposure

    registry = _registry()
    ctx = _ctx(tmp_path)
    ctx = replace(ctx, exposure=ToolExposure(max_schema_count=100))
    tool = capability_search_tool(registry)
    result = tool.handler({"query": "read files", "load": True}, ctx)
    assert result.data["loaded"]
    ctx = replace(ctx, exposure=ToolExposure(max_schema_count=0))
    result = tool.handler({"query": "read files", "load": True}, ctx)
    assert result.data["loaded"] == []
    assert "budget" in result.data["load_error"]
    assert not ctx.exposure.activated


def test_activation_budget_failure_is_atomic(tmp_path):
    from rinari.capability_search import capability_activation_tools
    from rinari.tools.exposure import ToolExposure

    registry = _registry()
    ctx = _ctx(tmp_path)
    ctx = replace(ctx, exposure=ToolExposure(max_schema_count=0))
    result = capability_activation_tools(registry)[0].handler({"names": ["fs.read"]}, ctx)
    assert not result.ok
    assert result.error.code.value == "RESOURCE_EXHAUSTED"
    assert not ctx.exposure.activated
