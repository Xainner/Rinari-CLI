"""Session-scoped tool exposure (Etapa B / review §4).

The model no longer receives the whole registry on every request. Each
request exposes::

    required core (always_loaded, non-lazy source)
    + explicitly activated tools (capability.activate / capability.search flow)
    + recently used tools (continuity across turns)

capped by a schema budget (count + estimated tokens). Budgets never drop
core or activated tools; when they alone exceed the budget the request
still carries them and metrics flag ``over_budget``.

Lazy sources (Nivel C: browser / MCP / OpenAPI / plugins) stay out of the
default view until activated or recently used. ``capability.search`` is
always exposed so the model can always recover the on-demand ecosystem.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# Manifest sources (or name prefixes) that are on-demand, never core.
LAZY_SOURCES = frozenset({"browser", "mcp", "openapi", "plugin"})

# Always exposed: the recovery path into the on-demand ecosystem.
SEARCH_TOOL = "capability.search"

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
    try:
        raw = json.dumps(
            getattr(tool, "input_schema", {}),
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError):
        raw = "{}"
    return max(1, len(raw) // 4)


@dataclass(slots=True)
class ToolExposure:
    """Mutable per-session exposure policy consulted by the agent loop."""

    max_schema_count: int = 96
    max_schema_tokens: int = 32_000
    activated: dict[str, Activation] = field(default_factory=dict)
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=_RECENT_MAX))
    requests_seen: int = 0
    activations_total: int = 0

    # -- mutation ------------------------------------------------------
    def activate(
        self,
        names: list[str],
        *,
        reason: str = "",
        scope: str = TURN_SCOPE,
        ttl_rounds: int = _DEFAULT_TTL_ROUNDS,
    ) -> list[str]:
        """Activate tools for the turn (ttl rounds) or the session."""
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
        removed = [str(n) for n in names if self.activated.pop(str(n), None) is not None]
        return removed

    def note_used(self, name: str) -> None:
        """Executed tools stay visible for continuity across turns."""
        name = str(name)
        if name in self.recent:
            self.recent.remove(name)
        self.recent.append(name)

    def prune(self) -> None:
        """Decay turn-scoped activations; called once per model request."""
        self.requests_seen += 1
        expired: list[str] = []
        for name, act in self.activated.items():
            if act.scope == TURN_SCOPE:
                act.rounds_left -= 1
                if act.rounds_left <= 0:
                    expired.append(name)
        for name in expired:
            del self.activated[name]

    # -- views ----------------------------------------------------------
    @staticmethod
    def _visible(registry: Any) -> list[Any]:
        tools: list[Any] = []
        for name in registry.names():
            tool = registry.get(name)
            if tool is not None:
                tools.append(tool)
        return tools

    def exposed_names(self, registry: Any) -> list[str]:
        """Ordered exposure: core + activated + recent, within schema budget."""
        tools = self._visible(registry)
        by_name = {t.name: t for t in tools}

        core = [t.name for t in tools if not is_lazy_tool(t)]
        if SEARCH_TOOL in by_name and SEARCH_TOOL not in core:
            core.insert(0, SEARCH_TOOL)
        wanted = list(core)
        for name in self.activated:
            if name in by_name and name not in wanted:
                wanted.append(name)
        for name in self.recent:
            if name in by_name and name not in wanted:
                wanted.append(name)

        # Schema budget: core + activated are promised, recent fills.
        promised = set(core) | set(self.activated)
        picked: list[str] = []
        tokens = 0
        for name in wanted:
            cost = _schema_tokens(by_name[name])
            if name not in promised and (
                len(picked) >= self.max_schema_count or tokens + cost > self.max_schema_tokens
            ):
                continue
            picked.append(name)
            tokens += cost
        return picked

    def for_model(self, registry: Any) -> tuple:
        """Wire-ready schemas for the current exposure."""
        return registry.for_model(self.exposed_names(registry))

    def metrics(self, registry: Any) -> dict[str, Any]:
        tools = self._visible(registry)
        exposed = set(self.exposed_names(registry))
        total_tokens = sum(_schema_tokens(t) for t in tools if t.name in exposed)
        promised_tokens = sum(
            _schema_tokens(t)
            for t in tools
            if t.name in exposed and (not is_lazy_tool(t) or t.name in self.activated)
        )
        return {
            "registered": len(tools),
            "exposed": len(exposed),
            "lazy": sum(1 for t in tools if is_lazy_tool(t)),
            "activated": sorted(self.activated),
            "recent": list(self.recent),
            "schema_tokens_est": total_tokens,
            "over_budget": (
                len(exposed) > self.max_schema_count or promised_tokens > self.max_schema_tokens
            ),
            "requests_seen": self.requests_seen,
            "activations_total": self.activations_total,
        }
