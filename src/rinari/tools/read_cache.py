"""Repeated reads of an unchanged file, answered with a pointer to the first one.

An audit of real sessions found the same unchanged file read in full again and
again (36 re-reads inside one turn, 73 inside one session), each one resending
the whole text to the model. When the exact text a read would return is still
in front of the model, the read now answers with a short note instead.

"Still in front of the model" is checked against the conversation itself, not
inferred from counters: the earlier tool message must still be in the history,
must reach the next request intact (settle.py replaces large old results with a
note), and must hold the very same text. Compaction clears the cache outright.
Anything that cannot be confirmed reads normally, so the cache can only save
context, never hide content. The file is still read every time: comparing the
fresh text is what makes "unchanged" a fact rather than a guess from mtimes.
"""

from __future__ import annotations

import io
import json
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

# Below this the full text costs about as much as the note: just send it.
MIN_DEDUPE_CHARS = 512
# Bounded memory: the newest reads per file, and the files read most recently.
_MAX_PER_PATH = 4
_MAX_PATHS = 512


def path_key(path: str) -> str:
    """Comparable form of a resolved path (case-insensitive where the OS is)."""
    return os.path.normcase(os.path.normpath(path))


@dataclass(frozen=True, slots=True)
class EarlierRead:
    tool_call_id: str
    tool: str
    calls_ago: int

    def note(self) -> str:
        return (
            f"Unchanged since your {self.tool} {self.calls_ago} call(s) ago: same content, and "
            "that text is still earlier in this conversation, so it was not sent again. "
            "Pass fresh=true if you need the full text here."
        )


class ReadCache:
    """Reads whose full observation one agent conversation received.

    One per AgentContext: a subagent has its own history, so it never inherits
    a parent's cache (it would point at text it never saw).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reads: OrderedDict[str, list[str]] = OrderedDict()
        self._revision: Any = None
        self._history_len = 0

    def view(self, agent_ctx: Any) -> ReadView:
        return ReadView(
            self,
            history=lambda: agent_ctx.history,
            revision=lambda: getattr(agent_ctx, "compact_revision", None),
        )

    def _sync(self, revision: Any, history_len: int) -> None:
        # Compaction rewrites what the model sees; a shorter history means
        # messages were dropped. Either way the old pointers are void.
        if revision != self._revision or history_len < self._history_len:
            self._reads.clear()
            self._revision = revision
        self._history_len = history_len

    def _record(self, key: str, call_id: str) -> None:
        calls = self._reads.pop(key, [])
        if call_id not in calls:
            calls.append(call_id)
        self._reads[key] = calls[-_MAX_PER_PATH:]
        while len(self._reads) > _MAX_PATHS:
            self._reads.popitem(last=False)


def for_agent(agent_ctx: Any) -> ReadView:
    """The read view for one agent conversation, creating its cache once."""
    cache = getattr(agent_ctx, "read_cache", None)
    if cache is None:
        cache = ReadCache()
        agent_ctx.read_cache = cache
    return cache.view(agent_ctx)


class ReadView:
    """What fs.read / fs.read_lines see through ToolContext.reads."""

    def __init__(
        self,
        cache: ReadCache,
        *,
        history: Callable[[], Sequence[Any]],
        revision: Callable[[], Any],
    ) -> None:
        self._cache = cache
        self._history = history
        self._revision = revision

    def record(self, path: str, call_id: str) -> None:
        """Remember that the observation of `call_id` carries `path`'s text."""
        if not call_id:
            return
        history = self._history()
        with self._cache._lock:
            self._cache._sync(self._revision(), len(history))
            self._cache._record(path_key(path), call_id)

    def earlier(self, path: str, matches: Callable[[str, dict], bool]) -> EarlierRead | None:
        """The newest earlier read of `path` the model still sees with this text.

        `matches(tool, data)` receives the earlier observation's data for this
        file and says whether it already holds what the new read would return.
        """
        key = path_key(path)
        history = tuple(self._history())
        with self._cache._lock:
            self._cache._sync(self._revision(), len(history))
            calls = list(self._cache._reads.get(key, ()))
        if not calls:
            return None
        wanted = set(calls)
        positions = {
            message.tool_call_id: index
            for index, message in enumerate(history)
            if getattr(message, "role", None) == "tool"
            and getattr(message, "tool_call_id", None) in wanted
        }
        from rinari.context.settle import kept_intact, settled_boundary

        boundary = settled_boundary(history)
        for call_id in reversed(calls):
            index = positions.get(call_id)
            if index is None:
                continue
            message = history[index]
            if not kept_intact(message, index, boundary):
                continue
            data = _observed_data(message.content or "", key)
            if data is None or not matches(message.name or "", data):
                continue
            later = sum(1 for m in history[index + 1 :] if getattr(m, "role", None) == "tool")
            return EarlierRead(call_id, message.name or "a read", later + 1)
        return None


def _observed_data(content: str, key: str) -> dict | None:
    """The data block for this file in a stored fs.read / fs.read_lines result."""
    try:
        envelope = json.loads(content)
    except (TypeError, ValueError):
        return None  # projected, spilled or otherwise not the raw envelope
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        return None
    rows = data.get("files")
    if isinstance(rows, list):  # fs.read with paths: one row per file
        for row in rows:
            row_data = row.get("data") if isinstance(row, dict) else None
            if (
                row.get("ok")
                and isinstance(row_data, dict)
                and isinstance(row_data.get("path"), str)
                and path_key(row_data["path"]) == key
            ):
                return row_data
        return None
    if envelope.get("ok") is not True or not isinstance(data.get("path"), str):
        return None
    return data if path_key(data["path"]) == key else None


def numbered_rows(text: str, start: int, end: int) -> str:
    """fs.read_lines rows for lines start..end of a whole-file text.

    Mirrors fs.read_lines: the same universal-newline splitting as a file
    opened with newline="", each row "N| line" without its line ending.
    """
    rows: list[str] = []
    stream = io.StringIO(text, newline="")
    number = 0
    while number < end:
        line = stream.readline()
        if not line:
            break
        number += 1
        if number >= start:
            rows.append(f"{number}| {line.rstrip(chr(13) + chr(10))}")
    return "\n".join(rows)


def holds_rows(earlier_rows: str, rows: str) -> bool:
    """Whether a block of whole "N| line" rows appears in an earlier result."""
    return ("\n" + earlier_rows + "\n").find("\n" + rows + "\n") >= 0


__all__ = [
    "MIN_DEDUPE_CHARS",
    "EarlierRead",
    "ReadCache",
    "ReadView",
    "for_agent",
    "holds_rows",
    "numbered_rows",
    "path_key",
]
