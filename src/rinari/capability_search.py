"""Unified capability search (phase 5).

Ranks the capabilities available to the session — native, plugin, MCP and
OpenAPI tools (they all converge in the same ToolRegistry) plus the browser
fallback — with a reliability x risk model.

Reliability follows the harness.md preference order (typed connector >
MCP > HTTP > browser DOM):

    native 1.00 > plugin 0.90 > openapi 0.85 > mcp 0.80 > web 0.75 > browser 0.70

High-risk capabilities are demoted so the model prefers the safe route.
`connector` tools are reserved: a connector source can be injected but none
ships in v1.
"""

from __future__ import annotations

from collections.abc import Callable

from rinari.tools.definition import (
    RISK_HIGH,
    RISK_MEDIUM,
    ClassifiedAction,
    ToolDefinition,
)
from rinari.tools.registry import ToolRegistry

SOURCE_NATIVE = "native"
SOURCE_PLUGIN = "plugin"
SOURCE_MCP = "mcp"
SOURCE_OPENAPI = "openapi"
SOURCE_BROWSER = "browser"
SOURCE_WEB = "web"
SOURCE_CONNECTOR = "connector"

_RELIABILITY: dict[str, float] = {
    SOURCE_NATIVE: 1.00,
    SOURCE_PLUGIN: 0.90,
    SOURCE_OPENAPI: 0.85,
    SOURCE_MCP: 0.80,
    SOURCE_WEB: 0.75,
    SOURCE_BROWSER: 0.70,
    SOURCE_CONNECTOR: 0.95,
}

_RISK_DEMOTION = {RISK_MEDIUM: 0.05, RISK_HIGH: 0.15}


def classify_source(tool: ToolDefinition) -> str:
    manifest = tool.manifest or {}
    if manifest.get("source") == "openapi":
        return SOURCE_OPENAPI
    if manifest.get("source") == "plugin":
        return SOURCE_PLUGIN
    if manifest.get("source") == "mcp":
        return SOURCE_MCP
    if tool.name.startswith("mcp."):
        return SOURCE_MCP
    if tool.name.startswith("api."):
        return SOURCE_OPENAPI
    if tool.name.startswith("browser."):
        return SOURCE_BROWSER
    if tool.name.startswith("web."):
        return SOURCE_WEB
    return SOURCE_NATIVE


def search_capabilities(
    registry: ToolRegistry,
    query: str,
    *,
    limit: int = 10,
    extra_sources: Callable[[], list[ToolDefinition]] | None = None,
) -> list[dict]:
    terms = [t.lower() for t in query.split() if t]
    candidates: list[ToolDefinition] = [
        tool for tool in (registry.get(name) for name in registry.names()) if tool is not None
    ]
    if extra_sources is not None:
        candidates.extend(extra_sources())
    scored: list[tuple[float, str, ToolDefinition]] = []
    seen: set[str] = set()
    for tool in candidates:
        if tool.name in seen:
            continue
        seen.add(tool.name)
        haystack = f"{tool.name} {tool.description}".lower()
        base = sum(haystack.count(term) for term in terms) if terms else 1
        if terms and base == 0:
            continue
        source = classify_source(tool)
        score = base * _RELIABILITY.get(source, 0.9) - _RISK_DEMOTION.get(tool.risk, 0.0)
        scored.append((score, tool.name, tool))
    scored.sort(key=lambda item: (-item[0], item[1]))
    results: list[dict] = []
    for score, _, tool in scored[: max(1, limit)]:
        results.append(
            {
                "name": tool.name,
                "source": classify_source(tool),
                "description": tool.description,
                "risk": tool.risk,
                "side_effects": tool.side_effects,
                "score": round(score, 3),
            }
        )
    if not results and terms:
        results.append(_browser_fallback(query))
    return results


def _browser_fallback(query: str) -> dict:
    return {
        "name": "browser.open",
        "source": SOURCE_BROWSER,
        "description": (
            f"No typed capability matched {query!r}; browser automation is the "
            "fallback route (slower, higher risk). Consider registering an "
            "OpenAPI spec or MCP server for this service."
        ),
        "risk": RISK_HIGH,
        "side_effects": "remote-reversible",
        "score": 0.0,
    }


def capability_search_tool(registry: ToolRegistry) -> ToolDefinition:
    def handler(arguments: dict, ctx) -> object:
        from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult

        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.INVALID_ARGUMENT, "query is required"),
                origin="capability",
            )
        if "limit" in arguments:
            try:
                limit = max(1, min(50, int(arguments.get("limit") or 10)))
            except (TypeError, ValueError):
                limit = 10
        else:
            limit = 10
        results = search_capabilities(registry, query, limit=limit)
        return ToolResult(ok=True, data={"query": query, "results": results}, origin="capability")

    return ToolDefinition(
        name="capability.search",
        description=(
            "Search the capabilities available this session across all sources "
            "(native, plugin, MCP, OpenAPI, browser fallback). Prefer typed "
            "API/connector matches over browser automation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you want to do (service, verb)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["query"],
        },
        capabilities=("state.read",),
        risk="low",
        idempotent=True,
        namespace="capability",
        manifest={"source": "native", "kind": "unified-capability-search"},
        classify=lambda _input: ClassifiedAction("state.read"),
        handler=handler,
    )


__all__ = [
    "SOURCE_BROWSER",
    "SOURCE_CONNECTOR",
    "SOURCE_MCP",
    "SOURCE_NATIVE",
    "SOURCE_OPENAPI",
    "SOURCE_PLUGIN",
    "SOURCE_WEB",
    "capability_search_tool",
    "classify_source",
    "search_capabilities",
]
