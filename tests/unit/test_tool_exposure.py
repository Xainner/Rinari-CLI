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


def test_the_list_is_decided_at_the_first_request_and_never_changes(
    registry: ToolRegistry,
) -> None:
    exp = ToolExposure()
    first = exp.for_model(registry)
    exp.activate(["browser.open"], scope="session")
    exp.note_used("mcp.x")
    exp.prune()
    assert exp.for_model(registry) == first
    assert exp.exposed_names(registry) == ["capability.search", "fs.read"]
    # A tool registered later does not join either.
    registry.register(_tool("fs.stat"))
    assert exp.for_model(registry) == first


def test_activations_before_the_list_is_decided_are_part_of_it(registry: ToolRegistry) -> None:
    exp = ToolExposure()
    exp.activate(["browser.open"], scope="session")
    assert [s.name for s in exp.for_model(registry)] == [
        "browser.open",
        "capability.search",
        "fs.read",
    ]


def test_unknown_activations_ignored(registry: ToolRegistry) -> None:
    exp = ToolExposure()
    exp.activate(["nope.missing"])
    assert "nope.missing" not in exp.exposed_names(registry)


def test_search_and_door_are_always_in_the_list() -> None:
    from rinari.tools.door import door_tool

    reg = ToolRegistry()
    reg.register_all(
        [_tool("capability.search", manifest={"source": "plugin"}), _tool("fs.read"), door_tool()]
    )
    assert set(ToolExposure().exposed_names(reg)) == {"capability.search", "fs.read", "tool.call"}


def test_the_list_is_kept_and_decided_again_only_when_its_key_moves(
    registry: ToolRegistry,
) -> None:
    kept = []
    key = {"engine": "x", "model": "m1", "compaction": 0}
    exp = ToolExposure(key=key, on_fix=lambda k, schemas: kept.append((k, schemas)))
    first = exp.for_model(registry)
    exp.for_model(registry)
    assert kept == [(key, first)]
    assert exp.follow(dict(key)) is False
    registry.register(_tool("fs.stat"))
    assert exp.follow({**key, "compaction": 1}) is True
    assert "fs.stat" in [s.name for s in exp.for_model(registry)]
    assert len(kept) == 2 and kept[1][0]["compaction"] == 1
    # A list with no key (a subagent's) never moves.
    child = ToolExposure()
    child.for_model(registry)
    assert child.follow(key) is False


def test_a_saved_list_is_sent_as_saved(registry: ToolRegistry) -> None:
    from rinari.models.types import ToolSchema

    saved = (ToolSchema("fs.read", "an older description", {"type": "object"}),)
    exp = ToolExposure(fixed=saved, key={"model": "m"})
    assert exp.for_model(registry) == saved
    assert exp.exposed_names(registry) == ["fs.read"]


def test_the_list_stays_under_the_tool_limit() -> None:
    from rinari.tools.exposure import MAX_FIXED_TOOLS

    reg = ToolRegistry()
    reg.register_all([_tool(f"pack.t{i:03d}") for i in range(MAX_FIXED_TOOLS + 20)])
    reg.register(_tool("capability.search"))
    names = ToolExposure().exposed_names(reg)
    assert len(names) == MAX_FIXED_TOOLS and "capability.search" in names


def test_for_model_returns_wire_schemas(registry: ToolRegistry) -> None:
    schemas = ToolExposure().for_model(registry)
    assert {s.name for s in schemas} == {"fs.read", "capability.search"}


def test_metrics_keys(registry: ToolRegistry) -> None:
    m = ToolExposure().metrics(registry)
    assert m["registered"] == 6
    assert m["exposed"] == 2
    assert m["lazy"] == 4
    assert m["activated"] == []
    assert m["fixed"] is False
    assert m["over_budget"] is False


def test_search_exact_match_wins(registry: ToolRegistry) -> None:
    results = search_capabilities(registry, "browser.open", limit=3)
    assert results and results[0]["name"] == "browser.open"


def test_activation_tools_give_the_call_and_leave_the_list(registry: ToolRegistry) -> None:
    tools = {t.name: t for t in capability_activation_tools(registry)}
    assert {"capability.activate", "capability.deactivate"} <= set(tools)

    class Ctx:
        exposure = ToolExposure()

    listed = Ctx.exposure.for_model(registry)
    ok = tools["capability.activate"].handler({"names": ["browser.open", "fs.read"]}, Ctx())
    assert ok.ok and ok.data["in_list"] == ["fs.read"]
    (outside,) = ok.data["outside_list"]
    assert outside["name"] == "browser.open"
    assert outside["call"] == {
        "tool": "tool.call",
        "arguments": {"name": "browser.open", "arguments": "{}"},
    }
    assert Ctx.exposure.for_model(registry) == listed

    bad = tools["capability.activate"].handler({"names": ["nope.missing"]}, Ctx())
    assert not bad.ok
    gone = tools["capability.deactivate"].handler({"names": ["fs.read"]}, Ctx())
    assert gone.ok and gone.data["deactivated"] == []


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

    listed = Ctx.exposure.for_model(reg)
    found = reg.get("capability.search").handler({"query": "lsp definition"}, Ctx())
    row = next(r for r in found.data["results"] if r["name"] == "lsp.definition")
    assert row["in_list"] is False
    assert row["how_to_call"]["call"]["tool"] == "tool.call"
    assert row["how_to_call"]["parameters"] == reg.get("lsp.definition").input_schema
    assert Ctx.exposure.for_model(reg) == listed


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
