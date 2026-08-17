"""Deterministic token accounting and context-pressure math (phase 4).

Token counts here are *estimates* (chars/4 over serialized message text),
never provider-reported numbers. They drive compaction pressure only; any
metric surfaced to the user still comes from real provider usage
(`models.types.Usage`).
"""

from __future__ import annotations

import json

from rinari.models.types import ChatMessage, ToolSchema

CHARS_PER_TOKEN = 4

# Harness trigger points (harness.md section 67).
PRESSURE_WARNING = 0.70
PRESSURE_PREPARE = 0.80
PRESSURE_COMPACT = 0.85

# Budget fallback when neither the provider capabilities nor a session-level
# override reports a context window. Deliberately conservative: compaction
# only engages on genuinely large histories.
DEFAULT_CONTEXT_WINDOW = 128_000

# After compaction the retained tail targets this share of the window,
# leaving headroom for system prompt and new output.
POST_COMPACT_KEEP_RATIO = 0.70


def _text_length(value: str | None) -> int:
    return len(value or "")


def estimate_message_tokens(message: ChatMessage) -> int:
    text = _text_length(message.content)
    if message.tool_calls:
        text += 16 + len(json.dumps([tc.name for tc in message.tool_calls]))
        text += sum(8 + len(json.dumps(tc.arguments, default=str)) for tc in message.tool_calls)
    text += _text_length(message.name) or 4
    if message.tool_call_id:
        text += len(message.tool_call_id)
    return max(1, text // CHARS_PER_TOKEN) + 2  # per-message role overhead


def estimate_tokens(
    *,
    system_prompt: str = "",
    history: tuple[ChatMessage, ...] | list[ChatMessage] = (),
    tools: tuple[ToolSchema, ...] | list[ToolSchema] = (),
) -> int:
    total = len(system_prompt) // CHARS_PER_TOKEN + 2
    for message in history:
        total += estimate_message_tokens(message)
    if tools:
        total += len(json.dumps([t.name for t in tools])) // CHARS_PER_TOKEN
        total += sum(len(json.dumps(t.parameters, default=str)) for t in tools) // (
            CHARS_PER_TOKEN * 2
        )
    return total


def resolve_context_window(provider_window: int | None) -> int:
    if provider_window is not None and provider_window > 0:
        return int(provider_window)
    return DEFAULT_CONTEXT_WINDOW


def pressure(used_tokens: int, window_tokens: int | None) -> float | None:
    """0..1 context pressure; None when the window is unknown."""
    if window_tokens is None or window_tokens <= 0 or used_tokens is None:
        return None
    return min(1.0, used_tokens / window_tokens)


__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_CONTEXT_WINDOW",
    "POST_COMPACT_KEEP_RATIO",
    "PRESSURE_COMPACT",
    "PRESSURE_PREPARE",
    "PRESSURE_WARNING",
    "estimate_message_tokens",
    "estimate_tokens",
    "pressure",
    "resolve_context_window",
]
