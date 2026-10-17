"""Tool calls as stored events, whatever their name (plan 0.4.0 #91).

Until 0.4.0 every call was stored twice: the runtime's `ToolRequested` /
`ToolCompleted` / `ToolFailed` and the activity's `tool.requested` /
`tool.completed` / `tool.failed` / `tool.cancelled`. The Engine now stores
only the activity ones (the request carries the full arguments in
`full_arguments`); the CLI, which stores no activity, keeps the old ones.
Readers go through here so old and new sessions count each call once.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

REQUESTED = ("tool.requested", "ToolRequested")
ENDED = ("tool.completed", "tool.failed", "tool.cancelled", "ToolCompleted", "ToolFailed")


def _type_and_payload(event: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(event, tuple):
        return str(event[0]), dict(event[1] or {})
    return str(event.type), dict(event.payload or {})


def requests(events: Iterable[Any]) -> list[dict[str, Any]]:
    """One request per call: `tool`, `tool_call_id`, full `arguments`, and
    `turn_index` when known. A stored activity request without its full
    arguments (before 0.4.0) gives way to the runtime's."""
    found: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        kind, payload = _type_and_payload(event)
        if kind not in REQUESTED:
            continue
        call = str(payload.get("tool_call_id") or "")
        if kind == "tool.requested":
            if not isinstance(payload.get("full_arguments"), dict):
                continue
            arguments = payload["full_arguments"]
        else:
            arguments = payload.get("arguments")
        if not call or not isinstance(arguments, dict) or call in found:
            continue
        found[call] = {
            "tool": payload.get("tool") or payload.get("name"),
            "tool_call_id": call,
            "arguments": arguments,
            "turn_index": payload.get("turn_index"),
        }
        order.append(call)
    return [found[call] for call in order]


def ends(events: Iterable[Any]) -> list[dict[str, Any]]:
    """One end per call: `tool`, `tool_call_id`, `ok`, `duration_ms`,
    `error_code`. Events without a call id (bookkeeping) are left out."""
    found: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        kind, payload = _type_and_payload(event)
        if kind not in ENDED:
            continue
        call = str(payload.get("tool_call_id") or "")
        if not call or call in found:
            continue
        error = payload.get("error")
        found[call] = {
            "tool": payload.get("tool") or payload.get("name") or "tool",
            "tool_call_id": call,
            "ok": bool(payload.get("ok")) and kind not in ("tool.failed", "tool.cancelled"),
            "duration_ms": payload.get("duration_ms"),
            "error_code": payload.get("error_code")
            or (error.get("code") if isinstance(error, dict) else None),
        }
        order.append(call)
    return [found[call] for call in order]
