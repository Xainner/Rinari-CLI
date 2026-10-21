"""The owner's permissions for an MCP server and its tools (plan de conectores, C5).

Kept with the server, in the `permissions` key of its `config_json` (no
migration, no secret):

```text
"permissions": {
  "default": "ask",                          Allow / Ask / Block for every tool
  "tools": {                                 by the server's own tool name
    "send_email": {"decision": "ask"},
    "search": {"annotations": {"readOnlyHint": true}}   the owner's correction
  }
}
```

A tool's setting wins over the server's default, and both win over the
mode's rule (`policy/mcp_tools.py`). An annotation corrected by the owner
replaces the server's hint; the others stay the server's.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from rinari.policy.mcp_tools import (
    ANNOTATION_KEYS,
    DECISIONS,
    SOURCE_MODE,
    SOURCE_SERVER,
    SOURCE_TOOL,
    McpToolFacts,
    effective_annotations,
    facts_for,
)

from .config import McpConfigError

PERMISSIONS_KEY = "permissions"
#: Longest tool name a setting is kept for (MCP names are short).
MAX_TOOL_NAME = 256
#: Settings kept per server, also for tools the server no longer lists.
MAX_TOOL_SETTINGS = 1000

_UNSET: Any = object()


def stored_permissions(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The permissions block of a stored config, cleaned (never raises)."""
    raw = (config or {}).get(PERMISSIONS_KEY)
    result: dict[str, Any] = {"default": None, "tools": {}}
    if not isinstance(raw, dict):
        return result
    if raw.get("default") in DECISIONS:
        result["default"] = raw["default"]
    tools = raw.get("tools")
    if isinstance(tools, dict):
        for name, spec in tools.items():
            if not isinstance(name, str) or not isinstance(spec, dict):
                continue
            entry: dict[str, Any] = {}
            if spec.get("decision") in DECISIONS:
                entry["decision"] = spec["decision"]
            annotations = spec.get("annotations")
            if isinstance(annotations, dict):
                kept = {
                    key: annotations[key]
                    for key in ANNOTATION_KEYS
                    if isinstance(annotations.get(key), bool)
                }
                if kept:
                    entry["annotations"] = kept
            if entry:
                result["tools"][name] = entry
    return result


def serialized(permissions: Mapping[str, Any]) -> dict[str, Any] | None:
    """What goes back into `config_json` (None when nothing is set)."""
    out: dict[str, Any] = {}
    if permissions.get("default") in DECISIONS:
        out["default"] = permissions["default"]
    tools = {name: dict(spec) for name, spec in sorted((permissions.get("tools") or {}).items())}
    if tools:
        out["tools"] = tools
    return out or None


def _decision(value: Any, what: str) -> str | None:
    if value is None:
        return None
    if value not in DECISIONS:
        raise McpConfigError(f"{what} must be allow, ask, deny or null")
    return value


def with_default(permissions: Mapping[str, Any], decision: Any) -> dict[str, Any]:
    """A copy with the server's default set (None clears it)."""
    result = copy.deepcopy(dict(permissions))
    result["default"] = _decision(decision, "Param 'decision'")
    return result


def with_tool_setting(
    permissions: Mapping[str, Any],
    tool: Any,
    *,
    decision: Any = _UNSET,
    annotations: Any = _UNSET,
) -> dict[str, Any]:
    """A copy with one tool's setting changed.

    `decision`: allow | ask | deny, or None to follow the server's default.
    `annotations`: `{readOnlyHint?, destructiveHint?, openWorldHint?}` with a
    boolean (the correction) or None (back to the server's hint) per key;
    `annotations: None` drops every correction. Left out, a field is kept.
    """
    if not isinstance(tool, str) or not tool.strip() or len(tool) > MAX_TOOL_NAME:
        raise McpConfigError("Param 'tool' must be the tool's name")
    result = copy.deepcopy(dict(permissions))
    tools: dict[str, Any] = dict(result.get("tools") or {})
    entry: dict[str, Any] = dict(tools.get(tool) or {})
    if decision is not _UNSET:
        value = _decision(decision, "Param 'decision'")
        if value is None:
            entry.pop("decision", None)
        else:
            entry["decision"] = value
    if annotations is not _UNSET:
        if annotations is None:
            entry.pop("annotations", None)
        else:
            if not isinstance(annotations, dict):
                raise McpConfigError("Param 'annotations' must be an object or null")
            unknown = sorted(set(annotations) - set(ANNOTATION_KEYS))
            if unknown:
                raise McpConfigError(
                    f"Param 'annotations' only corrects {', '.join(ANNOTATION_KEYS)}"
                    f" (got {', '.join(map(str, unknown))})"
                )
            corrected = dict(entry.get("annotations") or {})
            for key, value in annotations.items():
                if value is None:
                    corrected.pop(key, None)
                elif isinstance(value, bool):
                    corrected[key] = value
                else:
                    raise McpConfigError(f"annotations.{key} must be true, false or null")
            if corrected:
                entry["annotations"] = corrected
            else:
                entry.pop("annotations", None)
    if entry:
        tools[tool] = entry
    else:
        tools.pop(tool, None)
    if len(tools) > MAX_TOOL_SETTINGS:
        raise McpConfigError(f"at most {MAX_TOOL_SETTINGS} tool settings per server")
    result["tools"] = tools
    return result


def effective_decision(permissions: Mapping[str, Any], tool: str) -> tuple[str | None, str]:
    """`(decision, source)`: the tool's own setting, the server's, or the mode."""
    entry = (permissions.get("tools") or {}).get(tool) or {}
    if entry.get("decision") in DECISIONS:
        return entry["decision"], SOURCE_TOOL
    if permissions.get("default") in DECISIONS:
        return permissions["default"], SOURCE_SERVER
    return None, SOURCE_MODE


def tool_facts(
    server: str,
    tool: str,
    server_annotations: Mapping[str, Any] | None,
    permissions: Mapping[str, Any],
) -> McpToolFacts:
    corrections = ((permissions.get("tools") or {}).get(tool) or {}).get("annotations")
    decision, source = effective_decision(permissions, tool)
    return facts_for(
        server,
        tool,
        effective_annotations(dict(server_annotations or {}), corrections),
        decision=decision,
        decision_source=source,
    )


__all__ = [
    "PERMISSIONS_KEY",
    "effective_decision",
    "serialized",
    "stored_permissions",
    "tool_facts",
    "with_default",
    "with_tool_setting",
]
