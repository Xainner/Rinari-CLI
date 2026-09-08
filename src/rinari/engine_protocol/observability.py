"""Observability reads for desktop clients (Phase 10).

Usage, context/compaction state and artifact browsing — all derived from
persisted state, nothing estimated. Cost is reported only when the engine
knows pricing (it does not in v1: always null, never invented).
"""

from __future__ import annotations

from typing import Any

from rinari.context.compact_state import CompactState

DEFAULT_READ_BYTES = 64 * 1024
MAX_READ_BYTES = 1024 * 1024


def usage_from_events(events: list[Any]) -> dict[str, Any]:
    model_calls = 0
    tokens = {"input": 0, "output": 0, "cached": 0, "reasoning": 0}
    tool_calls = 0
    tool_ok = 0
    tool_err = 0
    for event in events:
        payload = event.payload or {}
        if event.type == "ModelInvoked":
            model_calls += 1
            usage = payload.get("usage") or {}
            tokens["input"] += usage.get("input_tokens") or 0
            tokens["output"] += usage.get("output_tokens") or 0
            tokens["cached"] += usage.get("cached_input_tokens") or 0
            tokens["reasoning"] += usage.get("reasoning_tokens") or 0
        elif event.type in ("ToolCompleted", "ToolFailed") and "tool_call_id" in payload:
            tool_calls += 1
            if payload.get("ok"):
                tool_ok += 1
            else:
                tool_err += 1
    return {
        "model_calls": model_calls,
        "tokens": tokens,
        "tool_calls": {"total": tool_calls, "ok": tool_ok, "error": tool_err},
        "cost": None,
    }


def context_view(session_id: str, compact_state: dict | None) -> dict[str, Any]:
    state = CompactState.from_dict(compact_state)
    data = state.to_dict()
    counts = {
        key: len(data.get(key) or [])
        for key in (
            "tasks_completed",
            "tasks_active",
            "tasks_blocked",
            "changed_files",
            "validations",
            "approvals",
            "artifacts",
        )
    }
    return {
        "session_id": session_id,
        "compacted": not state.is_empty(),
        "compacted_at": data.get("compacted_at") or "",
        "goal": data.get("goal") or "",
        "provider_model": data.get("provider_model") or "",
        "counts": counts,
    }


def clamp_read_bytes(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_READ_BYTES
    return max(1, min(number, MAX_READ_BYTES))
