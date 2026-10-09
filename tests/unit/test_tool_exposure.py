"""ToolExposure tests: core + activated + recent, schema budgets, activation tools."""

from __future__ import annotations

import pytest

from rinari.capability_search import (
    capability_activation_tools,
    search_capabilities,
)
from rinari.tools.definition import ToolDefinition
from rinari.tools.exposure import ToolExposure, is_lazy_tool
from rinari.tools.registry import ToolRegistry


def _tool(name: str, **kwargs) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} tool",
        input_schema={"type": "object", "properties": {}},
        **kwargs,
    )


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register_all(
        [
            _tool("fs.read"),
            _tool("capability.search"),
            _tool("browser.open", manifest={"source": "browser"}),
            _tool("mcp.x", manifest={"source": "mcp"}),
            _tool("api.y", manifest={"source": "openapi"}),
            _tool("lazy.flagged", always_loaded=False),
        ]
    )
    return reg


def test_lazy_by_source_and_flag() -> None:
    assert not is_lazy_tool(_tool("fs.read"))
    assert is_lazy_tool(_tool("browser.open", manifest={"source": "browser"}))
    assert is_lazy_tool(_tool("anything", always_loaded=False))


def test_default_view_is_core_only(registry: ToolRegistry) -> None:
    names = ToolExposure().exposed_names(registry)
    assert "fs.read" in names
    assert "capability.search" in names
    assert "browser.open" not in names
    assert "mcp.x" not in names
    assert "api.y" not in names
    assert "lazy.flagged" not in names


def test_search_always_exposed_even_if_lazy(registry: ToolRegistry) -> None:
    reg = ToolRegistry()
    reg.register_all(
        [
            _tool("capability.search", manifest={"source": "plugin"}),
            _tool("fs.read"),
        ]
    )
    assert "capability.search" in ToolExposure().exposed_names(reg)


def test_activate_and_deactivate(registry: ToolRegistry) -> None:
    exp = ToolExposure()
    assert exp.activate(["browser.open"], reason="need page") == ["browser.open"]
    assert "browser.open" in exp.exposed_names(registry)
    assert exp.deactivate(["browser.open"]) == ["browser.open"]
    assert "browser.open" not in exp.exposed_names(registry)


def test_activate_rejects_bad_scope() -> None:
    with pytest.raises(ValueError):
        ToolExposure().activate(["browser.open"], scope="task")


def test_turn_scope_decays_but_session_persists(registry: ToolRegistry) -> None:
    exp = ToolExposure()
    exp.activate(["browser.open"], scope="turn", ttl_rounds=2)
    exp.activate(["mcp.x"], scope="session")
    exp.prune()
    assert "browser.open" in exp.exposed_names(registry)
    exp.prune()
    assert "browser.open" not in exp.exposed_names(registry)
    assert "mcp.x" in exp.exposed_names(registry)


def test_recently_used_stays_visible(registry: ToolRegistry) -> None:
    exp = ToolExposure()
    exp.activate(["browser.open"], scope="turn", ttl_rounds=1)
    exp.note_used("browser.open")
    exp.prune()  # activation expired...
    assert "browser.open" in exp.exposed_names(registry)  # ...but recent keeps it


def test_unknown_activations_ignored(registry: ToolRegistry) -> None:
    exp = ToolExposure()
    exp.activate(["nope.missing"])
    assert "nope.missing" not in exp.exposed_names(registry)


def test_schema_budget_drops_recent_never_promised(registry: ToolRegistry) -> None:
    exp = ToolExposure(max_schema_count=2, max_schema_tokens=10**9)
    exp.activate(["browser.open"], scope="session")
    exp.note_used("mcp.x")
    names = exp.exposed_names(registry)
    # core(fs.read, capability.search)=2 fills count; activated browser.open is
    # promised and kept; recent mcp.x is dropped.
    assert "fs.read" in names
    assert "capability.search" in names
    assert "browser.open" in names
    assert "mcp.x" not in names
    assert exp.metrics(registry)["over_budget"] is True


def test_for_model_returns_wire_schemas(registry: ToolRegistry) -> None:
    schemas = ToolExposure().for_model(registry)
    assert {s.name for s in schemas} == {"fs.read", "capability.search"}


def test_metrics_keys(registry: ToolRegistry) -> None:
    m = ToolExposure().metrics(registry)
    assert m["registered"] == 6
    assert m["exposed"] == 2
    assert m["lazy"] == 4
    assert m["activated"] == []
    assert m["over_budget"] is False


def test_search_exact_match_wins(registry: ToolRegistry) -> None:
    results = search_capabilities(registry, "browser.open", limit=3)
    assert results and results[0]["name"] == "browser.open"


def test_activation_tools_roundtrip(registry: ToolRegistry) -> None:
    tools = {t.name: t for t in capability_activation_tools(registry)}
    assert {"capability.activate", "capability.deactivate"} <= set(tools)

    class Ctx:
        exposure = ToolExposure()

    ok = tools["capability.activate"].handler({"names": ["browser.open"]}, Ctx())
    assert ok.ok and ok.data["activated"] == ["browser.open"]
    assert "browser.open" in Ctx.exposure.exposed_names(registry)

    bad = tools["capability.activate"].handler({"names": ["nope.missing"]}, Ctx())
    assert not bad.ok

    class NoExposure:
        pass

    err = tools["capability.activate"].handler({"names": ["browser.open"]}, NoExposure())
    assert not err.ok


def test_schema_cost_counts_the_description_the_provider_also_receives() -> None:
    from rinari.tools.exposure import _schema_tokens

    terse = _tool("x.tool")
    verbose = ToolDefinition(
        name="x.tool",
        description="d" * 4000,
        input_schema={"type": "object", "properties": {}},
    )
    assert _schema_tokens(verbose) >= 1000 > _schema_tokens(terse) * 10


def test_rarely_used_native_packs_are_on_demand_but_searchable() -> None:
    from rinari.capability_search import capability_search_tool
    from rinari.tools.native import all_native_tools

    reg = ToolRegistry()
    reg.register_all(all_native_tools())
    reg.register(capability_search_tool(reg))
    exposed = set(ToolExposure().exposed_names(reg))
    on_demand = {"lsp.", "pty.", "context.", "artifact.metadata", "artifact.export"}
    assert not {name for name in exposed if name.startswith(tuple(on_demand))}
    # Every spilled observation points at artifact.read: it stays core.
    assert {"artifact.read", "shell.exec", "fs.read", "capability.search"} <= exposed

    class Ctx:
        exposure = ToolExposure()
        lsp = object()  # a session with a language server

    found = reg.get("capability.search").handler({"query": "lsp definition", "load": True}, Ctx())
    assert "lsp.definition" in found.data["loaded"]
    assert "lsp.definition" in Ctx.exposure.exposed_names(reg)


def test_a_child_agent_sees_the_on_demand_tools_its_definition_names() -> None:
    from rinari.agents.definition import AgentDefinition
    from rinari.agents.runtime import expose_allowlisted

    reg = ToolRegistry()
    reg.register_all(
        [_tool("lsp.hover", always_loaded=False), _tool("pty.start", always_loaded=False)]
    )
    named = AgentDefinition(name="types", description="d", objective="o", tool_allowlist=("lsp.*",))
    exposure = ToolExposure()
    expose_allowlisted(named, reg, exposure)
    assert exposure.exposed_names(reg) == ["lsp.hover"]
    # No allowlist means "everything available", not "activate everything".
    open_ended = AgentDefinition(name="any", description="d", objective="o")
    exposure = ToolExposure()
    expose_allowlisted(open_ended, reg, exposure)
    assert exposure.exposed_names(reg) == []
