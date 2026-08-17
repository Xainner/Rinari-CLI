"""Tool registry: lookup, search, describe, load/unload, manifest."""

from __future__ import annotations

from rinari.models.types import ToolSchema
from rinari.tools.definition import ToolDefinition


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._hidden: set[str] = set()

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def register_all(self, tools: list[ToolDefinition]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> ToolDefinition | None:
        tool = self._tools.get(name)
        if tool is None or name in self._hidden:
            return None
        return tool

    def names(self) -> list[str]:
        return sorted(n for n in self._tools if n not in self._hidden)

    def search(self, query: str) -> list[ToolDefinition]:
        terms = [t for t in query.lower().split() if t]
        results: list[tuple[int, ToolDefinition]] = []
        for tool in self._tools.values():
            if tool.name in self._hidden:
                continue
            haystack = f"{tool.name} {tool.description}".lower()
            score = sum(haystack.count(term) for term in terms)
            if score > 0:
                results.append((score, tool))
        results.sort(key=lambda pair: (-pair[0], pair[1].name))
        return [tool for _, tool in results]

    def describe(self, name: str) -> dict | None:
        tool = self._tools.get(name)
        if tool is None:
            return None
        return self._manifest_entry(tool)

    def for_model(self, names: list[str] | None = None) -> tuple[ToolSchema, ...]:
        selected = names if names is not None else self.names()
        return tuple(
            self._tools[name].to_model_schema() for name in selected if name in self._tools
        )

    def manifests(self) -> dict:
        namespaces: dict[str, list[str]] = {}
        for tool in self._tools.values():
            if tool.name in self._hidden:
                continue
            namespaces.setdefault(tool.namespace, []).append(tool.name)
        entries = [{"name": n, "tools": sorted(t)} for n, t in sorted(namespaces.items())]
        return {"namespaces": entries}

    @staticmethod
    def _manifest_entry(tool: ToolDefinition) -> dict:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "capabilities": list(tool.capabilities),
            "permissions": list(tool.permissions),
            "risk": tool.risk,
            "side_effects": tool.side_effects,
            "idempotent": tool.idempotent,
            "timeout_ms": tool.timeout_ms,
            "max_output_bytes": tool.max_output_bytes,
            "always_loaded": tool.always_loaded,
            "manifest": tool.manifest,
        }
