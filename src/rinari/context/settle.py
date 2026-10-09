"""Old tool observations, settled into short notes in the model request.

Every model call used to resend every earlier tool result in full: in real
sessions one file read was sent again 94 times in a turn, and resent
observations were the single largest share of input tokens. The model rarely
needs the raw text of a read it acted on many steps ago, and it can always
run the tool again.

This only shapes the request. The persisted history keeps every result, and
compaction still decides what the conversation remembers. Results settle in
blocks of whole rounds, so the request changes at one point every few rounds
instead of every call and the provider's prompt cache keeps the prefix.
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

_REFERENCE = re.compile(r"artifact://[^\s\"'`)\]}]+")


def settle_old_observations(
    history: Sequence[ChatMessage],
    *,
    keep_rounds: int = KEEP_ROUNDS,
    block_rounds: int = BLOCK_ROUNDS,
    min_chars: int = MIN_CHARS,
) -> list[ChatMessage]:
    """The history with large results of old tool rounds replaced by a note."""
    rounds = [i for i, m in enumerate(history) if m.role == "assistant" and m.tool_calls]
    settled = max(0, (len(rounds) - keep_rounds) // block_rounds * block_rounds)
    if not settled:
        return list(history)
    # Tool results answer the round before them; everything before the first
    # kept round belongs to a settled one.
    boundary = rounds[settled]
    result: list[ChatMessage] = []
    for index, message in enumerate(history):
        if index < boundary and message.role == "tool" and len(message.content or "") >= min_chars:
            message = replace(message, content=_note(message))
        result.append(message)
    return result


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
