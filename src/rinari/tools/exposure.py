"""The conversation's tool list: decided once, never changed in the middle (F42).

Measured against real providers, changing the tool list once in the middle of
a conversation stops the prompt cache until it ends, and a local model reads
the whole prompt again. So the list is decided at the first request of the
conversation and stays byte for byte the same in every later request and
turn, until a point where the cache is lost anyway: another model or
provider, or a compaction (the session host decides those, see
`cli.agent_runtime._exposure_for`).

The list holds the native core (tools not flagged ``always_loaded=False`` and
not from an on-demand source), ``capability.search`` and the door
``tool.call``, in the registry's order. Everything else (browser, MCP,
OpenAPI, plugins, the on-demand native packs) is found with
``capability.search`` and run through the door; a tool called by its exact
name still runs, because the registry, not the list, resolves calls.

Activations made before the list is decided are part of it: that is how a
subagent's definition gets the tools it names. Activations after that are
recorded, never added: the list does not change.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rinari.models.types import ToolSchema

# Manifest sources (or name prefixes) that are on-demand, never core.
LAZY_SOURCES = frozenset({"browser", "mcp", "openapi", "plugin"})

# Always in the list: the way to find, and the way to run, what is not.
SEARCH_TOOL = "capability.search"
DOOR_TOOL = "tool.call"

# Requests over this many tools are refused by OpenAI Chat Completions and by
# servers that copy its limit (others accept more): the list stays under it.
MAX_FIXED_TOOLS = 128

TURN_SCOPE = "turn"
SESSION_SCOPE = "session"
_SCOPES = (TURN_SCOPE, SESSION_SCOPE)

_DEFAULT_TTL_ROUNDS = 4
_RECENT_MAX = 16


@dataclass(slots=True)
class Activation:
    scope: str = TURN_SCOPE
    rounds_left: int = _DEFAULT_TTL_ROUNDS
    reason: str = ""


def _source_of(tool: Any) -> str:
    manifest = getattr(tool, "manifest", None) or {}
    source = str(manifest.get("source") or "").strip().lower()
    if source:
        return source
    name = str(getattr(tool, "name", ""))
    for prefix in ("browser.", "mcp.", "api.", "plugin."):
        if name.startswith(prefix):
            return prefix.rstrip(".")
    return "native"


def is_lazy_tool(tool: Any) -> bool:
    """A tool is lazy when flagged or from an on-demand source."""
    if not bool(getattr(tool, "always_loaded", True)):
        return True
    return _source_of(tool) in LAZY_SOURCES


def _schema_tokens(tool: Any) -> int:
    """Estimated wire cost of one tool: name + description + input schema."""
    try:
        raw = json.dumps(
            getattr(tool, "input_schema", None) or getattr(tool, "parameters", {}),
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError):
        raw = "{}"
    text = str(getattr(tool, "name", "")) + str(getattr(tool, "description", "") or "")
    return max(1, (len(text) + len(raw)) // 4)


def list_key(model_ref: str | None, compaction: int | None) -> dict[str, Any]:
    """What a list is decided for: the model, the compaction and the Engine.

    An Engine update may change the tools themselves, so it decides again too.
    """
    from rinari import __version__

    return {"engine": __version__, "model": model_ref or "", "compaction": int(compaction or 0)}


def schemas_to_json(schemas: tuple[ToolSchema, ...]) -> list[dict[str, Any]]:
    return [
        {"name": s.name, "description": s.description, "parameters": s.parameters} for s in schemas
    ]


def schemas_from_json(rows: Any) -> tuple[ToolSchema, ...] | None:
    if not isinstance(rows, list) or not rows:
        return None
    try:
        return tuple(
            ToolSchema(
                name=str(row["name"]),
                description=str(row.get("description") or ""),
                parameters=dict(row.get("parameters") or {}),
            )
            for row in rows
        )
    except (KeyError, TypeError, AttributeError):
        return None


@dataclass(slots=True)
class ToolExposure:
    """The conversation's fixed tool list, decided at its first request."""

    #: The list once decided; loaded from the session when it is still valid.
    fixed: tuple[ToolSchema, ...] | None = None
    #: What the list was decided for (model, compaction, Engine version).
    key: dict[str, Any] | None = None
    #: Called with (key, list) when the list is decided here, to keep it.
    on_fix: Callable[[dict[str, Any] | None, tuple[ToolSchema, ...]], None] | None = None
    activated: dict[str, Activation] = field(default_factory=dict)
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=_RECENT_MAX))
    requests_seen: int = 0
    activations_total: int = 0
    registry: Any = None

    # -- mutation ------------------------------------------------------
    def activate(
        self,
        names: list[str],
        *,
        reason: str = "",
        scope: str = TURN_SCOPE,
        ttl_rounds: int = _DEFAULT_TTL_ROUNDS,
    ) -> list[str]:
        """Record an activation; it joins the list only before it is decided."""
        if scope not in _SCOPES:
            raise ValueError(f"unknown activation scope: {scope!r}")
        ttl = max(1, min(32, int(ttl_rounds)))
        added: list[str] = []
        for name in names:
            self.activated[str(name)] = Activation(scope=scope, rounds_left=ttl, reason=reason)
            added.append(str(name))
        self.activations_total += len(added)
        return added

    def deactivate(self, names: list[str]) -> list[str]:
        return [str(n) for n in names if self.activated.pop(str(n), None) is not None]

    def note_used(self, name: str) -> None:
        """Kept for metrics only: use never moves or adds a tool."""
        name = str(name)
        if name in self.recent:
            self.recent.remove(name)
        self.recent.append(name)

    def prune(self) -> None:
        """Count one model request; the list itself does not decay."""
        self.requests_seen += 1

    def follow(self, key: dict[str, Any]) -> bool:
        """Decide the list again when its key moved (another model, a compaction).

        Those points lose the cache anyway, so a new list costs nothing more.
        A list with no key (a subagent's) never moves.
        """
        if self.key is None or self.key == key:
            return False
        self.key = dict(key)
        self.fixed = None
        self.activated.clear()
        return True

    # -- views ----------------------------------------------------------
    @staticmethod
    def _visible(registry: Any) -> list[Any]:
        from rinari.tools.availability import availability

        tools: list[Any] = []
        for name in registry.names():
            tool = registry.get(name)
            if (
                tool is not None
                and availability(name, getattr(registry, "context", None))["available"] is not False
            ):
                tools.append(tool)
        return tools

    def _planned(self, registry: Any) -> list[str]:
        """Core + search + door + early activations, in the registry's order."""
        tools = self._visible(registry)
        always = {SEARCH_TOOL, DOOR_TOOL}
        wanted = {
            t.name
            for t in tools
            if not is_lazy_tool(t) or t.name in always or t.name in self.activated
        }
        names = [t.name for t in tools if t.name in wanted]
        if len(names) > MAX_FIXED_TOOLS:
            kept = [n for n in names if n in always]
            rest = [n for n in names if n not in always][: MAX_FIXED_TOOLS - len(kept)]
            names = [n for n in names if n in set(kept) | set(rest)]
        return names

    def exposed_names(self, registry: Any) -> list[str]:
        self.registry = registry
        if self.fixed is not None:
            return [schema.name for schema in self.fixed]
        return self._planned(registry)

    def in_list(self, name: str, registry: Any = None) -> bool:
        registry = registry if registry is not None else self.registry
        if self.fixed is None and registry is None:
            return False
        return name in self.exposed_names(registry)

    def for_model(self, registry: Any) -> tuple[ToolSchema, ...]:
        """The list for this request: decided on the first call, then the same."""
        self.registry = registry
        if self.fixed is None:
            self.fixed = registry.for_model(self._planned(registry))
            if self.on_fix is not None:
                self.on_fix(self.key, self.fixed)
        return self.fixed

    def metrics(self, registry: Any) -> dict[str, Any]:
        tools = self._visible(registry)
        exposed = self.exposed_names(registry)
        schemas = self.fixed if self.fixed is not None else registry.for_model(exposed)
        return {
            "registered": len(tools),
            "exposed": len(exposed),
            "lazy": sum(1 for t in tools if is_lazy_tool(t)),
            "fixed": self.fixed is not None,
            "activated": sorted(self.activated),
            "recent": list(self.recent),
            "schema_tokens_est": sum(_schema_tokens(s) for s in schemas),
            "over_budget": len(exposed) > MAX_FIXED_TOOLS,
            "requests_seen": self.requests_seen,
            "activations_total": self.activations_total,
        }
