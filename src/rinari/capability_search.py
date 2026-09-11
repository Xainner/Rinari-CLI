"""Unified capability search (phase 5).

Ranks the capabilities available to the session — native, plugin, MCP and
OpenAPI tools (they all converge in the same ToolRegistry), including registered
browser tools — with a reliability x risk model.

Reliability follows the harness.md preference order (typed connector >
MCP > HTTP > browser DOM):

    native 1.00 > plugin 0.90 > openapi 0.85 > mcp 0.80 > web 0.75 > browser 0.70

High-risk capabilities are demoted so the model prefers the safe route.
`connector` tools are reserved: a connector source can be injected but none
ships in v1.
"""

from __future__ import annotations

import re
import unicodedata
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

_INTENT_TERMS = {
    "archivos": "file",
    "archivo": "file",
    "leer": "read",
    "buscar": "search",
    "carpeta": "directory",
    "directorios": "directory",
    "navegador": "browser",
    "remoto": "ssh",
    "servidor": "ssh",
    "hardware": "hardware",
    "memoria": "memory",
    "proceso": "process",
    "terminal": "pty",
    "verificar": "verify",
}


def _terms(text: str) -> list[str]:
    normalized = "".join(
        c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c)
    )
    words = re.findall(r"[\w.-]+", normalized)
    return list(dict.fromkeys([*words, *(_INTENT_TERMS[w] for w in words if w in _INTENT_TERMS)]))


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
    terms = _terms(query)
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
        # A verbose description must not win merely by repeating keywords.
        base = (
            sum((3 if term in tool.name.lower() else 1) for term in terms if term in haystack)
            if terms
            else 1
        )
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
    return results


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
        from rinari.tools.availability import availability

        for item in results:
            item.update(availability(item["name"], ctx))
        loaded = []
        load_error = None
        if arguments.get("load") and getattr(ctx, "exposure", None) is not None:
            names = [item["name"] for item in results if item["available"] is not False]
            from rinari.tools.exposure import _schema_tokens

            visible = set(ctx.exposure.exposed_names(registry)) | set(names)
            if (
                len(visible) <= ctx.exposure.max_schema_count
                and sum(_schema_tokens(registry.get(n)) for n in visible)
                <= ctx.exposure.max_schema_tokens
            ):
                loaded = ctx.exposure.activate(names, reason=query, scope="turn")
            else:
                load_error = (
                    "Schema budget exceeded; request fewer matches or deactivate unused tools."
                )
        elif arguments.get("load"):
            load_error = "Dynamic exposure is not enabled in this context."
        return ToolResult(
            ok=True,
            data={
                "query": query,
                "results": results,
                "matched": bool(results),
                "loaded": loaded,
                "load_error": load_error,
                "diagnostics": list(registry.diagnostics),
                "guidance": (
                    "Use an exact returned tool name."
                    if results
                    else "No registered capability matched. Check the integration configuration; "
                    "do not repeat the same search without new information."
                ),
            },
            origin="capability",
        )

    return ToolDefinition(
        name="capability.search",
        description=(
            "Search the capabilities available this session across all sources "
            "(native, plugin, MCP, OpenAPI and browser). Prefer typed "
            "API/connector matches over browser automation. Set load=true to "
            "activate matching schemas in the same call, within the schema budget."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you want to do (service, verb)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "load": {"type": "boolean", "default": False},
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
        from rinari.tools.exposure import _schema_tokens

        visible = set(exposure.exposed_names(registry)) | set(names)
        if (
            len(visible) > exposure.max_schema_count
            or sum(_schema_tokens(registry.get(n)) for n in visible) > exposure.max_schema_tokens
        ):
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(
                    ToolErrorCode.RESOURCE_EXHAUSTED,
                    "Schema budget exceeded; activate fewer tools or deactivate unused tools.",
                ),
                origin="capability",
            )
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
        removed = exposure.deactivate(names)
        return ToolResult(
            ok=True,
            data={
                "deactivated": removed,
                "still_exposed": [
                    name for name in names if name in exposure.exposed_names(registry)
                ],
                "note": (
                    "Core or recently used tools may remain visible "
                    "after explicit activation is removed."
                ),
            },
            origin="capability",
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
