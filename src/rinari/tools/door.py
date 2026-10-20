"""The door: one fixed tool that carries calls to tools outside the list (F42).

A conversation keeps the same tool list from its first request to the next
point where its cache is lost anyway (model switch, compaction): any change in
the middle stops the provider's prompt cache for the rest of the conversation.
Tools outside that list are found with capability.search, whose result gives
the schema and the exact call, and run through `tool.call`. The schema travels
in the history, which only grows at the end.

The door is only the transport. The loop unwraps a `tool.call` before anything
else sees it, so policy, approvals, the timeline and the logs see the real
tool name and its arguments, validated with that tool's own schema.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from rinari.models.types import ToolCall
from rinari.tools.definition import (
    ClassifiedAction,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)
from rinari.tools.exposure import DOOR_TOOL

# Matches offered when the requested name is not a registered tool.
_MATCHES = 3


def door_tool() -> ToolDefinition:
    def handler(_arguments: dict, _ctx) -> ToolResult:
        # The loop unwraps the door before running anything; reaching this
        # handler means a caller skipped that step.
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                ToolErrorCode.INVALID_ARGUMENT,
                "tool.call is unwrapped by the agent loop; call the tool by its name here.",
            ),
            origin="capability",
        )

    return ToolDefinition(
        name=DOOR_TOOL,
        description=(
            "Run a tool that is not in your tool list, by its exact name. Find it with "
            "capability.search: its result gives the tool's schema and the exact call to "
            "copy. `arguments` is the tool's arguments as one JSON object written as text, "
            'for example "{\\"path\\": \\"notes.md\\"}". A tool that is in your list is '
            "called directly."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact tool name, e.g. browser.open."},
                "arguments": {
                    "type": "string",
                    "description": "The tool's arguments: a JSON object as text ('{}' for none).",
                },
            },
            "required": ["name", "arguments"],
        },
        capabilities=("state.read",),
        risk="low",
        idempotent=False,
        namespace="tool",
        manifest={"source": "native", "kind": "tool-door"},
        classify=lambda _input: ClassifiedAction("state.read"),
        handler=handler,
    )


def _example(schema: dict[str, Any]) -> dict[str, Any]:
    """The required arguments with an empty value of their type, in schema order."""
    placeholders = {"string": "", "integer": 0, "number": 0, "boolean": False, "array": []}
    properties = schema.get("properties") or {}
    example: dict[str, Any] = {}
    for key in schema.get("required") or ():
        spec = properties.get(key) or {}
        kind = spec.get("type")
        if isinstance(kind, list):
            kind = next((k for k in kind if k != "null"), None)
        if spec.get("enum"):
            example[key] = spec["enum"][0]
        else:
            example[key] = placeholders.get(kind, {})
    return example


def exact_call(tool: ToolDefinition) -> dict[str, Any]:
    """The `tool.call` arguments that run `tool`, ready to fill and copy."""
    return {
        "name": tool.name,
        "arguments": json.dumps(_example(tool.input_schema or {}), ensure_ascii=False),
    }


def how_to_call(tool: ToolDefinition) -> dict[str, Any]:
    """Schema and exact call of one tool outside the list: deterministic, no ids."""
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema,
        "call": {"tool": DOOR_TOOL, "arguments": exact_call(tool)},
    }


def door_text(tools: list[ToolDefinition]) -> str:
    """The same block for the same tools, every time: it lives in the history."""
    blocks = []
    for tool in tools:
        call = json.dumps(exact_call(tool), ensure_ascii=False, sort_keys=True)
        schema = json.dumps(tool.input_schema or {}, ensure_ascii=False, sort_keys=True)
        blocks.append(
            f"- {tool.name}: {tool.description}\n  parameters: {schema}\n  call: {DOOR_TOOL} {call}"
        )
    return "\n".join(blocks)


def _matches(name: str, registry) -> list[str]:
    from rinari.capability_search import search_capabilities

    names = [n for n in registry.names() if n != DOOR_TOOL]
    close = difflib.get_close_matches(name, names, n=_MATCHES, cutoff=0.5)
    query = name.replace(".", " ").replace("_", " ")
    searched = [row["name"] for row in search_capabilities(registry, query, limit=_MATCHES * 2)]
    return list(dict.fromkeys([*close, *(n for n in searched if n != DOOR_TOOL)]))[:_MATCHES]


def _error(message: str, details: dict[str, Any]) -> ToolResult:
    return ToolResult(
        ok=False,
        error=ToolErrorInfo(ToolErrorCode.INVALID_ARGUMENT, message, True, details),
        origin="capability",
    )


def unwrap(call: ToolCall, registry) -> tuple[ToolCall, ToolResult | None]:
    """The real call a `tool.call` carries, or the error that explains how to fix it.

    Any other call comes back unchanged. The id stays the same, so the
    result still answers the model's own call.
    """
    if call.name != DOOR_TOOL:
        return call, None
    from rinari.tools.runtime import resolve_tool_name

    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    requested = str(arguments.get("name") or "").strip()
    failed = ToolCall(id=call.id, name=requested or DOOR_TOOL, arguments=arguments)
    if call.arguments_invalid or not requested:
        return failed, _error(
            "tool.call needs `name` (the exact tool name) and `arguments` (a JSON object as "
            "text); find the tool and its exact call with capability.search.",
            {"call": {"name": "<tool name>", "arguments": "{}"}},
        )
    tool = registry.get(requested)
    if tool is None:
        resolved = resolve_tool_name(requested, registry.names())
        tool = registry.get(resolved) if resolved else None
    if tool is None or tool.name == DOOR_TOOL:
        matches = _matches(requested, registry)
        found = [registry.get(n) for n in matches if registry.get(n) is not None]
        return failed, _error(
            f"Unknown tool {requested!r} for tool.call. Closest: "
            + (", ".join(t.name for t in found) or "none")
            + ". Use one of them as shown, or search with capability.search.",
            {"matches": [how_to_call(t) for t in found]},
        )
    raw = arguments.get("arguments")
    if raw is None or raw == "":
        parsed: Any = {}
    elif isinstance(raw, dict):
        parsed = raw
    else:
        try:
            parsed = json.loads(str(raw))
        except ValueError as exc:
            return replace_name(failed, tool.name), _error(
                f"arguments for {tool.name} is not valid JSON ({exc.msg} at position {exc.pos}); "
                "send one JSON object as text.",
                how_to_call(tool),
            )
    if not isinstance(parsed, dict):
        return replace_name(failed, tool.name), _error(
            f"arguments for {tool.name} must be a JSON object, not {type(parsed).__name__}.",
            how_to_call(tool),
        )
    return ToolCall(id=call.id, name=tool.name, arguments=parsed), None


def replace_name(call: ToolCall, name: str) -> ToolCall:
    return ToolCall(id=call.id, name=name, arguments=call.arguments)


__all__ = [
    "DOOR_TOOL",
    "door_text",
    "door_tool",
    "exact_call",
    "how_to_call",
    "unwrap",
]
