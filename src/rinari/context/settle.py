"""Old tool observations, settled into short notes in the model request.

Every model call used to resend every earlier tool result in full: in real
sessions one file read was sent again 94 times in a turn, and resent
observations were the single largest share of input tokens. The model rarely
needs the raw text of a read it acted on many steps ago, and it can always
run the tool again.

This only shapes the request. The persisted history keeps every result, and
compaction still decides what the conversation remembers.

When settling happens (F45, plan 0.4.0 v3). Moving the boundary rewrites the
middle of the request, and everything after it is read again: a small cache
loss in the cloud, tens of thousands of tokens of prefill on a local model.
So, like Anthropic's context editing (`trigger` + `clear_at_least`), nothing
settles while the request fits comfortably; once it passes SETTLE_AT of the
window, the old rounds settle in one jump down to SETTLE_TO, and the boundary
stays there until the next crossing. Without a known window the old fixed
blocks of rounds remain, as a fallback.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

from rinari.models.types import ChatMessage

# The newest rounds stay intact: the model is still reasoning about them.
KEEP_ROUNDS = 8
# Older rounds settle this many at a time (one cache break per block).
BLOCK_ROUNDS = 8
# Short results cost little and often carry the whole answer: kept.
MIN_CHARS = 1200
# Settle only past this share of the context window, then down to the other
# in one jump (F45): one cache break every long while, not one per block.
SETTLE_AT = 0.60
SETTLE_TO = 0.35

_REFERENCE = re.compile(r"artifact://[^\s\"'`)\]}]+")


class SettleState:
    """Where this conversation's settled part ends, kept between calls (F45).

    The boundary is a history index; the message just before it is remembered
    so a history rewritten by compaction (or restored) starts over at 0.
    """

    def __init__(self) -> None:
        self.boundary = 0
        self._anchor: int | None = None
        #: Whether the boundary follows context pressure (the window is known).
        self.by_pressure = False

    def current(self, history: Sequence[ChatMessage]) -> int:
        if not self.boundary:
            return 0
        if self.boundary > len(history) or id(history[self.boundary - 1]) != self._anchor:
            self.boundary, self._anchor = 0, None
        return self.boundary

    def move_to(self, boundary: int, history: Sequence[ChatMessage]) -> None:
        self.boundary = boundary
        self._anchor = id(history[boundary - 1]) if boundary else None


def plan_boundary(
    history: Sequence[ChatMessage],
    state: SettleState,
    *,
    window_tokens: int | None,
    fixed_tokens: int = 0,
    keep_rounds: int = KEEP_ROUNDS,
    min_chars: int = MIN_CHARS,
    settle_at: float = SETTLE_AT,
    settle_to: float = SETTLE_TO,
) -> int:
    """The boundary for the next request: unchanged until the request passes
    `settle_at` of the window, then moved once so it drops to `settle_to`.

    `fixed_tokens` is what the request carries besides the history (system
    prompt, tools). Without a known window the fixed blocks of rounds apply.
    """
    if not window_tokens or window_tokens <= 0:
        state.by_pressure = False
        return settled_boundary(history, keep_rounds=keep_rounds)
    state.by_pressure = True
    from rinari.context.tokens import estimate_message_tokens

    boundary = state.current(history)
    sizes = [estimate_message_tokens(message) for message in history]
    settled = [
        estimate_message_tokens(replace(message, content=_note(message)))
        if message.role == "tool" and len(message.content or "") >= min_chars
        else size
        for message, size in zip(history, sizes, strict=True)
    ]
    used = fixed_tokens + sum(settled[:boundary]) + sum(sizes[boundary:])
    if used < settle_at * window_tokens:
        return boundary
    rounds = [i for i, m in enumerate(history) if m.role == "assistant" and m.tool_calls]
    movable = rounds[: max(0, len(rounds) - keep_rounds)]
    target = settle_to * window_tokens
    moved = boundary
    for candidate in movable:
        if candidate <= moved:
            continue
        used -= sum(sizes[moved:candidate]) - sum(settled[moved:candidate])
        moved = candidate
        if used <= target:
            break
    if moved != boundary:
        state.move_to(moved, history)
    return moved


def settle_old_observations(
    history: Sequence[ChatMessage],
    *,
    boundary: int | None = None,
    keep_rounds: int = KEEP_ROUNDS,
    block_rounds: int = BLOCK_ROUNDS,
    min_chars: int = MIN_CHARS,
) -> list[ChatMessage]:
    """The history with large results of old tool rounds replaced by a note.

    `boundary` comes from `plan_boundary`; without it the fixed blocks apply.
    """
    if boundary is None:
        boundary = settled_boundary(history, keep_rounds=keep_rounds, block_rounds=block_rounds)
    if not boundary:
        return list(history)
    result: list[ChatMessage] = []
    for index, message in enumerate(history):
        if index < boundary and message.role == "tool" and len(message.content or "") >= min_chars:
            message = replace(message, content=_note(message))
        result.append(message)
    return result


def settled_boundary(
    history: Sequence[ChatMessage],
    *,
    keep_rounds: int = KEEP_ROUNDS,
    block_rounds: int = BLOCK_ROUNDS,
) -> int:
    """Index of the first message the next request keeps intact (0: all of them).

    Large tool results before it are settled into a note. The read cache asks
    the same question so it never points the model at text it no longer sees.
    """
    rounds = [i for i, m in enumerate(history) if m.role == "assistant" and m.tool_calls]
    settled = max(0, (len(rounds) - keep_rounds) // block_rounds * block_rounds)
    # Tool results answer the round before them; everything before the first
    # kept round belongs to a settled one.
    return rounds[settled] if settled else 0


def kept_intact(
    message: ChatMessage, index: int, boundary: int, min_chars: int = MIN_CHARS
) -> bool:
    """Whether the message at `index` reaches the next request unchanged."""
    return index >= boundary or message.role != "tool" or len(message.content or "") < min_chars


def _note(message: ChatMessage) -> str:
    text = message.content or ""
    references = list(dict.fromkeys(_REFERENCE.findall(text)))[:3]
    note = (
        f"[Earlier result of {message.name or 'a tool'} ({len(text)} characters) left out "
        "of this request to save context; you already acted on it. Run the tool again "
        "if you need its exact content."
    )
    if references:
        note += " Full output: " + ", ".join(references) + " (artifact.read)."
    return note + "]"
