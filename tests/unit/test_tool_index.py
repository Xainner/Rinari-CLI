"""The model sees what tools exist (plan 0.4.0 v2, V2-6) and the service the
owner chose goes first (V2-1)."""

from __future__ import annotations

from types import SimpleNamespace

from rinari.capability_search import capability_search_tool
from rinari.prompts.assembler import AssemblerContext, PromptAssembler
from rinari.tools.definition import RISK_LOW, SIDE_EFFECT_NONE, ToolDefinition, ToolResult
from rinari.tools.index import MAX_NAMES, deferred_index
from rinari.tools.native.image_create import image_create_tools
from rinari.tools.registry import ToolRegistry


def _tool(name: str, *, namespace: str | None = None, loaded=False, manifest=None, text="t"):
    return ToolDefinition(
        name=name,
        description=text,
        input_schema={"type": "object", "properties": {}},
        risk=RISK_LOW,
        side_effects=SIDE_EFFECT_NONE,
        handler=lambda args, ctx: ToolResult(ok=True, data={}),
        namespace=namespace or name.split(".")[0],
        always_loaded=loaded,
        manifest=manifest or {},
    )


def _registry(*tools) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def test_the_index_lists_every_deferred_tool_from_any_source_and_is_stable() -> None:
    registry = _registry(
        _tool("fs.read", loaded=True),
        _tool("documents.create"),
        _tool("documents.read"),
        _tool("pdf.redact"),
        _tool("acme.deploy", manifest={"source": "plugin"}),
        _tool(
            "mcp.Lab.generate_image",
            namespace="mcp.Lab",
            manifest={"source": "mcp", "server_instructions": "Wait with wait_for_job."},
        ),
    )
    text = deferred_index(registry)
    assert text is not None
    assert "fs.read" not in text  # already loaded
    assert "- documents: documents.create, documents.read" in text
    assert "- pdf: pdf.redact" in text
    assert "- acme: acme.deploy" in text
    assert "- mcp.Lab: mcp.Lab.generate_image" in text
    assert "Server note (from the server, not from the owner): Wait with wait_for_job." in text
    assert deferred_index(registry) == text
    assert deferred_index(_registry(_tool("fs.read", loaded=True))) is None


def test_a_large_group_is_clipped() -> None:
    many = [_tool(f"mcp.Big.t{i:02d}", namespace="mcp.Big") for i in range(MAX_NAMES + 4)]
    text = deferred_index(_registry(*many))
    assert text.endswith(", and 4 more")


def test_the_index_rides_in_the_system_prompt() -> None:
    bundle = PromptAssembler().build(AssemblerContext(tool_index="- documents: documents.create"))
    assert "- documents: documents.create" in bundle.system_prompt
    assert bundle.turn_context_message is None or "documents.create" not in (
        bundle.turn_context_message.content or ""
    )


def _host(label):
    return SimpleNamespace(service=SimpleNamespace(default_label=lambda: label))


def test_image_tools_load_and_name_the_service_the_owner_chose() -> None:
    chosen = image_create_tools(_host("Suscripción de ChatGPT (Codex)"))
    assert all(tool.always_loaded for tool in chosen)
    assert all("Suscripción de ChatGPT (Codex)" in tool.description for tool in chosen)
    assert chosen[0].manifest["configured_service"] == "Suscripción de ChatGPT (Codex)"
    none = image_create_tools(_host(None))
    assert not any(tool.always_loaded for tool in none)
    assert "configured_service" not in none[0].manifest


def test_search_puts_the_chosen_service_before_a_skill_or_an_mcp_server() -> None:
    tools = image_create_tools(_host("Suscripción de ChatGPT (Codex)"))
    mcp = [
        _tool(
            f"mcp.Lab.{name}",
            namespace="mcp.Lab",
            manifest={"source": "mcp"},
            text="generate image with the lab",
        )
        for name in ("generate_image", "edit_image", "review_image")
    ]
    registry = _registry(*tools, *mcp)
    skills = [{"name": "lab-image", "description": "generate image with the lab", "active": False}]
    search = capability_search_tool(registry, skills=lambda: skills)
    result = search.handler({"query": "generate image"}, SimpleNamespace(exposure=None))
    assert result.data["results"][0]["name"] == "image.generate"
    assert "The owner chose a service for this: use image.generate" in result.data["guidance"]


def test_activating_past_the_budget_unloads_this_turns_oldest_tools() -> None:
    """A large MCP server hit «Schema budget exceeded» on its third tool while
    finished ones stayed loaded (F33, plan 0.4.0 v2, V2-12)."""
    from rinari.capability_search import capability_activation_tools
    from rinari.tools.exposure import ToolExposure

    lazy = [
        _tool(f"mcp.Big.t{i}", namespace="mcp.Big", manifest={"source": "mcp"}) for i in range(4)
    ]
    registry = _registry(_tool("fs.read", loaded=True), *lazy)
    activate = capability_activation_tools(registry)[0]
    core = len([n for n in registry.names() if registry.get(n).always_loaded])
    exposure = ToolExposure(max_schema_count=core + 2)
    exposure.activate(["mcp.Big.t0"], reason="pinned", scope="session")
    ctx = SimpleNamespace(exposure=exposure)

    assert activate.handler({"names": ["mcp.Big.t1"]}, ctx).ok
    result = activate.handler({"names": ["mcp.Big.t2"]}, ctx)

    assert result.ok, result.error
    assert result.data["unloaded_to_make_room"] == ["mcp.Big.t1"]
    assert "mcp.Big.t0" in exposure.activated  # a session activation stays
    too_many = activate.handler({"names": ["mcp.Big.t2", "mcp.Big.t3"]}, ctx)
    assert not too_many.ok and "mcp.Big.t2" in exposure.activated
