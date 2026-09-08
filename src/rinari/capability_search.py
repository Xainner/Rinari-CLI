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
        if tool.name == query:
            score += 10  # exact tool-name match wins outright
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


def capability_activation_tools(registry: ToolRegistry) -> list[ToolDefinition]:
    """capability.activate / capability.deactivate over the session exposure.

    Explicit, traceable activation (review §4, Etapa B): the model searches
    with capability.search, then activates exactly what it needs. Turn-scoped
    activations decay automatically; session scope persists for the session.
    """

    def _exposure(ctx):
        from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult

        exposure = getattr(ctx, "exposure", None)
        if exposure is None:
            return None, ToolResult(
                ok=False,
                error=ToolErrorInfo(
                    ToolErrorCode.INVALID_ARGUMENT,
                    "dynamic exposure is not enabled in this context",
                ),
                origin="capability",
            )
        return exposure, None

    def _names(arguments: dict) -> list[str]:
        raw = arguments.get("names") or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(n).strip() for n in raw if str(n).strip()]

    def activate_handler(arguments: dict, ctx) -> object:
        from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult

        exposure, err = _exposure(ctx)
        if err is not None:
            return err
        names = _names(arguments)
        if not names:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.INVALID_ARGUMENT, "names is required"),
                origin="capability",
            )
        unknown = [n for n in names if registry.get(n) is None]
        if unknown:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(
                    ToolErrorCode.INVALID_ARGUMENT,
                    f"unknown tools: {', '.join(unknown)}; search with capability.search",
                ),
                origin="capability",
            )
        scope = str(arguments.get("scope") or "turn")
        if scope not in ("turn", "session"):
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(
                    ToolErrorCode.INVALID_ARGUMENT, "scope must be 'turn' or 'session'"
                ),
                origin="capability",
            )
        try:
            ttl = max(1, min(32, int(arguments.get("ttl_rounds") or 4)))
        except (TypeError, ValueError):
            ttl = 4
        reason = str(arguments.get("reason") or "")
        activated = exposure.activate(names, reason=reason, scope=scope, ttl_rounds=ttl)
        return ToolResult(
            ok=True,
            data={"activated": activated, "scope": scope, "reason": reason},
            origin="capability",
        )

    def deactivate_handler(arguments: dict, ctx) -> object:
        from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult

        exposure, err = _exposure(ctx)
        if err is not None:
            return err
        names = _names(arguments)
        if not names:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.INVALID_ARGUMENT, "names is required"),
                origin="capability",
            )
        return ToolResult(
            ok=True, data={"deactivated": exposure.deactivate(names)}, origin="capability"
        )

    return [
        ToolDefinition(
            name="capability.activate",
            description=(
                "Expose on-demand tools (browser, MCP, OpenAPI, plugins) to this "
                "session. Search first with capability.search, then activate exactly "
                "what the task needs. Turn scope lasts a few model rounds, session "
                "scope persists."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exact tool names to expose.",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["turn", "session"],
                        "default": "turn",
                    },
                    "ttl_rounds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 32,
                        "default": 4,
                        "description": "Model rounds a turn-scoped activation survives.",
                    },
                    "reason": {"type": "string", "description": "Why the task needs these."},
                },
                "required": ["names"],
            },
            capabilities=("state.mutate",),
            risk="low",
            idempotent=False,
            namespace="capability",
            manifest={"source": "native", "kind": "capability-activation"},
            classify=lambda _input: ClassifiedAction("state.mutate"),
            handler=activate_handler,
        ),
        ToolDefinition(
            name="capability.deactivate",
            description="Drop previously activated tools from this session's exposure.",
            input_schema={
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["names"],
            },
            capabilities=("state.mutate",),
            risk="low",
            idempotent=False,
            namespace="capability",
            manifest={"source": "native", "kind": "capability-activation"},
            classify=lambda _input: ClassifiedAction("state.mutate"),
            handler=deactivate_handler,
        ),
    ]


__all__ = [
    "SOURCE_BROWSER",
    "SOURCE_CONNECTOR",
    "SOURCE_MCP",
    "SOURCE_NATIVE",
    "SOURCE_OPENAPI",
    "SOURCE_PLUGIN",
    "SOURCE_WEB",
    "capability_activation_tools",
    "capability_search_tool",
    "classify_source",
    "search_capabilities",
]
