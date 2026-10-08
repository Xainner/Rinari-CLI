"""Frame size: one protocol line must always fit the desktop host.

The desktop reads stdout as NDJSON and drops the whole connection when a single
line exceeds its limit. A response that large is never useful to a UI, so the
Engine bounds what it sends instead: history pages shrink to a byte budget, an
oversized event loses the tail of its longest strings, and an oversized
response becomes a structured error for that one request.
"""

from __future__ import annotations

import json
from typing import Any

# Same ceiling as the desktop host (`MAX_LINE_BYTES` in NdjsonTransport.ts).
MAX_FRAME_BYTES = 16 * 1024 * 1024
# Pages of persisted conversation stay well below the frame: the envelope and
# a busy event stream share the same pipe.
PAGE_BUDGET_BYTES = 8 * 1024 * 1024
# Progressively shorter strings until an item fits; the original stays stored.
_STRING_CAPS = (1024 * 1024, 256 * 1024, 64 * 1024, 16 * 1024, 4 * 1024)


def encoded_size(value: Any) -> int:
    """Bytes on the wire: `json.dumps` escapes non-ASCII, so chars == bytes."""
    return len(json.dumps(value))


def _shrink(value: Any, cap: int) -> Any:
    if isinstance(value, str):
        if len(value) <= cap:
            return value
        omitted = len(value) - cap
        if value.startswith("data:"):
            # A cut data URL is not an image; say what was there instead.
            return f"[image omitted: {len(value)} characters]"
        return value[:cap] + f"\n\n[… {omitted} characters omitted: too large to show here]"
    if isinstance(value, dict):
        return {key: _shrink(item, cap) for key, item in value.items()}
    if isinstance(value, list):
        return [_shrink(item, cap) for item in value]
    return value


def fit(value: Any, budget: int) -> Any:
    """`value` unchanged when it fits; otherwise with its long strings cut."""
    if encoded_size(value) <= budget:
        return value
    candidate = value
    for cap in _STRING_CAPS:
        candidate = _shrink(value, cap)
        if encoded_size(candidate) <= budget:
            return candidate
    return candidate


def newest_within(items: list[Any], budget: int = PAGE_BUDGET_BYTES) -> list[Any]:
    """The newest items whose total size fits `budget`, in their original order.

    The newest item is always kept (shrunk if it alone exceeds the budget), so
    a session always opens on its latest turn.
    """
    kept: list[Any] = []
    used = 0
    for item in reversed(items):
        if not kept:
            item = fit(item, budget)
        size = encoded_size(item) + 1
        if kept and used + size > budget:
            break
        kept.append(item)
        used += size
    kept.reverse()
    return kept
