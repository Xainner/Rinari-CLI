"""History selection for compaction (phase 4).

The retained tail must be a *safe* conversation slice: the loop's wire format
is `user -> assistant(tool_calls) -> tool* -> assistant ...`, so cutting in
the middle of a tool block produces requests with orphaned `tool` messages
that providers reject. A cut therefore advances forward past any `tool`
message until it lands on a `user` or `assistant` boundary.

`select_history` returns the longest suffix that fits the token budget while
keeping at least `min_keep` messages (when affordable).
"""

from __future__ import annotations

from rinari.context.tokens import estimate_message_tokens
from rinari.models.types import ROLE_TOOL, ChatMessage


def _safe_cut(history: list[ChatMessage], index: int) -> int:
    """Advance `index` past any orphaned tool-message boundary."""
    n = len(history)
    while 0 < index < n and history[index].role == ROLE_TOOL:
        index += 1
    return min(index, n)


def select_history(
    history: list[ChatMessage], *, budget_tokens: int, min_keep: int = 6
) -> list[ChatMessage]:
    """Longest safe suffix of `history` whose estimate fits `budget_tokens`.

    Full history is returned unchanged when it already fits. When even
    `min_keep` messages exceed the budget, the single most recent message is
    kept (degenerate, but never crashes and never orphans a tool block).
    """
    n = len(history)
    if n == 0:
        return []
    toks = [estimate_message_tokens(m) for m in history]
    total = sum(toks)
    if total <= budget_tokens:
        return list(history)

    # Longest affordably-sized suffix: smallest cut with suffix <= budget,
    # bounded so at least min_keep messages survive.
    max_cut = max(0, n - min_keep)
    prefix = 0
    cut = max_cut  # worst case inside the bound
    for i in range(0, max_cut + 1):
        suffix = total - prefix
        if suffix <= budget_tokens:
            cut = i
            break
        prefix += toks[i]
    else:
        # Even the final min_keep messages overflow the budget.
        return list(history[n - 1 :])

    cut = _safe_cut(history, cut)
    if cut >= n:
        cut = n - 1
    return list(history[cut:])


__all__ = ["select_history"]
