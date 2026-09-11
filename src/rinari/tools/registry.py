"""Tool registry: lookup, search, describe, load/unload, manifest."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

from rinari.models.types import ToolSchema
from rinari.tools.definition import ToolDefinition


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._hidden: set[str] = set()
        self.diagnostics: list[dict[str, str]] = []
        self.context = None

    @contextmanager
    def loading(self, source: str):
        """Keep a failed optional source visible without exposing exception secrets."""
        try:
            yield
        except Exception as exc:
            self.diagnostics.append(
                {
                    "source": source,
                    "status": "unavailable",
                    "error": type(exc).__name__,
                    "next_action": "Check this integration's configuration.",
                }
            )

    def register(self, tool: ToolDefinition) -> None:
        previous = self._tools.get(tool.name)
        if previous is not None and previous.manifest.get("source", "native") != tool.manifest.get(
            "source", "native"
        ):
            raise ValueError(f"Tool name collision across sources: {tool.name}")
        from rinari.tools.output_contracts import output_schema

        if tool.output_schema is None and tool.manifest.get("source") not in {
            "plugin",
            "mcp",
            "openapi",
        }:
            tool = replace(tool, output_schema=output_schema(tool.name))
        if (
            tool.side_effects != "none"
            and tool.name.split(".")[0]
            in {
                "fs",
                "shell",
                "process",
                "pty",
                "browser",
                "memory",
                "context",
                "agent",
                "skills",
                "verify",
            }
            and tool.manifest.get("source") not in {"plugin", "mcp", "openapi"}
        ):
            schema = dict(tool.input_schema)
            schema["properties"] = {
                **schema.get("properties", {}),
                "request_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": (
                        "Optional deduplication key; reuse only for the same operation "
                        "in this live runtime (last 256 requests)."
                    ),
                },
            }
            tool = replace(
                tool, input_schema=schema, manifest={**tool.manifest, "supports_request_id": True}
            )
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
        from rinari.capability_search import search_capabilities

        return [
            self.get(row["name"])
            for row in search_capabilities(self, query, limit=len(self.names()))
        ]

    def describe(self, name: str) -> dict | None:
        tool = self._tools.get(name)
        if tool is None:
            return None
        return self._manifest_entry(tool)

    def for_model(self, names: list[str] | None = None) -> tuple[ToolSchema, ...]:
        from rinari.tools.availability import availability

        selected = names if names is not None else self.names()
        return tuple(
            self._tools[name].to_model_schema()
            for name in selected
            if name in self._tools and availability(name, self.context)["available"] is not False
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
        from rinari.tools.availability import availability

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
            "availability": availability(tool.name),
        }
