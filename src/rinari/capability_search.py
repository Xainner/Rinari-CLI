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
    "sesion": "session",
    "sesiones": "session",
    "turno": "turn",
    "turnos": "turn",
    "configuracion": "status",
    "diagnostico": "turn",
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
        if (tool.manifest or {}).get("configured_service"):
            # Backed by a service the owner chose (V2-1): it goes before a
            # skill's or an MCP server's tool that does the same.
            score += 5
        scored.append((score, tool.name, tool))
    # By match, then by name: never by which server connected or registered
    # first (C6). Rounded so float noise cannot break a tie.
    scored.sort(key=lambda item: (-round(item[0], 6), item[1]))
    results: list[dict] = []
    for score, _, tool in scored[: max(1, limit)]:
        row = {
            "name": tool.name,
            "source": classify_source(tool),
            "description": tool.description,
            "risk": tool.risk,
            "side_effects": tool.side_effects,
            "score": round(score, 3),
        }
        server = (tool.manifest or {}).get("server")
        if server and row["source"] == SOURCE_MCP:
            row["server"] = str(server)
        results.append(row)
    return results


# Results outside the list that come back with their schema and exact call:
# enough to act in the next step, without paying every schema of a long list.
_CALLS_IN_SEARCH = 3


def _outside_list(ctx, registry: ToolRegistry, name: str) -> bool:
    exposure = getattr(ctx, "exposure", None)
    if exposure is None:
        return False
    return not exposure.in_list(name, registry)


def _add_calls(results: list[dict], ctx, registry: ToolRegistry) -> int:
    """Mark each result in or out of the list; the first ones out get their call."""
    from rinari.tools.door import how_to_call

    given = 0
    for item in results:
        tool = registry.get(item["name"])
        outside = tool is not None and _outside_list(ctx, registry, item["name"])
        item["in_list"] = not outside
        if outside and given < _CALLS_IN_SEARCH and item.get("available") is not False:
            item["how_to_call"] = how_to_call(tool)
            given += 1
    return given


def _mark_waiting_servers(results: list[dict], ctx) -> set[str]:
    """Mark the results of MCP servers waiting for a new sign-in (C6).

    Their tools stay findable so the model can tell the owner what is wrong,
    but they come back unavailable and without a call. Returns those servers.
    """
    from rinari.tools.index import unavailable_reason

    service = getattr(ctx, "mcp", None)
    if service is None or not hasattr(service, "connectors"):
        return set()
    try:
        rows = {
            str(row["name"]): row
            for row in service.connectors(
                getattr(ctx, "project_root", None), getattr(ctx, "session_id", None)
            )
        }
    except Exception:
        return set()
    reauth: set[str] = set()
    for item in results:
        row = rows.get(str(item.get("server") or ""))
        if row is None or row.get("status") != "reauth_required":
            continue
        reauth.add(str(row["name"]))
        item.update(
            {
                "available": False,
                "reason": unavailable_reason(str(row["name"]), "reauth_required"),
                "requires": ["reconnect"],
            }
        )
    return reauth


def search_skills(skills, query: str, limit: int = 5) -> list[dict]:
    """Skills whose name or description match: a procedure beats a tool hunt."""
    terms = _terms(query)
    if not terms:
        return []
    scored = []
    for row in skills:
        name = str(row.get("name") or "")
        haystack = f"{name} {row.get('description') or ''}".lower()
        score = sum((3 if term in name.lower() else 1) for term in terms if term in haystack)
        if score:
            scored.append((score, name, row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        {
            "name": name,
            "source": "skill",
            "description": str(row.get("description") or "")[:300],
            "active": bool(row.get("active")),
            "how_to_use": f"skills.activate with name={name!r}, then follow its procedure",
            "score": score,
        }
        for score, name, row in scored[:limit]
    ]


def capability_search_tool(registry: ToolRegistry, skills=None) -> ToolDefinition:
    """`skills`: zero-arg callable returning the session's skill summaries."""

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
        reconnect = _mark_waiting_servers(results, ctx)
        # The tool list never changes (F42): what is outside it comes back
        # with its schema and the exact tool.call to run it.
        given = _add_calls(results, ctx, registry)
        skill_matches: list[dict] = []
        if skills is not None:
            try:
                skill_matches = search_skills(skills(), query)
            except Exception:
                skill_matches = []
        configured = next(
            (
                item
                for item in results
                if (registry.get(item["name"]) is not None)
                and (registry.get(item["name"]).manifest or {}).get("configured_service")
            ),
            None,
        )
        native = [item["name"] for item in results if item.get("source") == "native"][:3]
        if configured is not None:
            service = registry.get(configured["name"]).manifest["configured_service"]
            guidance = (
                f"The owner chose a service for this: use {configured['name']} ({service}). "
                "Use a skill or an MCP server for it only if the owner names one."
            )
        elif skill_matches:
            guidance = (
                f"A skill covers this: activate {skill_matches[0]['name']!r} with skills.activate "
                "and follow its procedure before improvising with tools."
            )
            if native:
                guidance += " Typed tools that match: " + ", ".join(native) + "."
        elif reconnect and results[0].get("server") in reconnect:
            guidance = (
                f"The best match needs its MCP server {results[0]['server']} reconnected: tell "
                "the owner to reconnect it in Settings → Connectors instead of trying another way."
            )
        elif results:
            guidance = (
                "Call a tool marked in_list directly; run any other through tool.call exactly "
                "as its how_to_call shows."
                if given
                else "Use an exact returned tool name."
            )
        else:
            guidance = (
                "No registered capability matched. Check the integration configuration; "
                "do not repeat the same search without new information."
            )
        return ToolResult(
            ok=True,
            data={
                "query": query,
                "skills": skill_matches,
                "results": results,
                "matched": bool(results or skill_matches),
                "diagnostics": list(registry.diagnostics),
                "guidance": guidance,
            },
            origin="capability",
        )

    return ToolDefinition(
        name="capability.search",
        description=(
            "Search the capabilities available this session across all sources "
            "(skills, native, plugin, MCP, OpenAPI and browser). A matching skill is a "
            "ready procedure: activate it first. Prefer typed "
            "API/connector matches over browser automation. A tool outside your list comes "
            "back with its schema and the exact tool.call that runs it."
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
    """capability.activate / capability.deactivate, kept for what names them.

    The tool list never changes in the middle of a conversation (F42), so
    activating is finding: it returns each tool's schema and the exact
    tool.call that runs it, and deactivating has nothing to undo. Skills and
    instructions written for the old flow keep working.
    """

    def _names(arguments: dict) -> list[str]:
        raw = arguments.get("names") or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(n).strip() for n in raw if str(n).strip()]

    def activate_handler(arguments: dict, ctx) -> object:
        from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult
        from rinari.tools.door import how_to_call

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
        in_list = [n for n in names if not _outside_list(ctx, registry, n)]
        outside = [how_to_call(registry.get(n)) for n in names if n not in in_list]
        data: dict = {"in_list": in_list, "outside_list": outside}
        data["note"] = (
            "Your tool list does not change. Call the in_list tools directly; run the others "
            "through tool.call exactly as each call shows."
            if outside
            else "These tools are already in your list: call them directly."
        )
        return ToolResult(ok=True, data=data, origin="capability")

    def deactivate_handler(arguments: dict, ctx) -> object:
        from rinari.tools.definition import ToolResult

        return ToolResult(
            ok=True,
            data={
                "deactivated": [],
                "note": "Your tool list does not change; there is nothing to deactivate.",
            },
            origin="capability",
        )

    return [
        ToolDefinition(
            name="capability.activate",
            description=(
                "Get the schema and the exact tool.call of tools outside your list, by exact "
                "name (find them with capability.search). Your tool list does not change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exact tool names.",
                    },
                    "reason": {"type": "string", "description": "Why the task needs these."},
                },
                "required": ["names"],
            },
            capabilities=("state.read",),
            risk="low",
            idempotent=True,
            namespace="capability",
            manifest={"source": "native", "kind": "capability-activation"},
            classify=lambda _input: ClassifiedAction("state.read"),
            handler=activate_handler,
        ),
        ToolDefinition(
            name="capability.deactivate",
            description="Kept for old instructions: your tool list does not change.",
            input_schema={
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["names"],
            },
            capabilities=("state.read",),
            risk="low",
            idempotent=True,
            namespace="capability",
            always_loaded=False,
            manifest={"source": "native", "kind": "capability-activation"},
            classify=lambda _input: ClassifiedAction("state.read"),
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
