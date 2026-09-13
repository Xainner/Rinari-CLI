"""Shared, correlated output events for parent and child runtimes."""

import contextlib
import threading

from rinari.shared.execution_scope import tool_output_context


def activity_output_sink(activity_sink, live_sink=None):
    """Fan process output to the REPL and structured activity events.

    ToolRuntime binds the current tool identity around this callback. Process
    readers run on their own threads, so the identity is carried by that
    bound closure instead of inferred from the worker thread. Stream sequence
    and character offsets let clients replay events without duplicating text.
    """

    if activity_sink is None and live_sink is None:
        return None
    offsets: dict[tuple[str, str], int] = {}
    sequences: dict[tuple[str, str], int] = {}
    lock = threading.Lock()

    def emit(stream: str, text: str) -> None:
        if not text:
            return
        identity = tool_output_context.get()
        if not identity:
            if live_sink is not None:
                with contextlib.suppress(Exception):
                    live_sink(stream, text)
            return
        tool, tool_call_id = identity
        key = (str(tool_call_id), stream)
        if live_sink is not None:
            with contextlib.suppress(Exception):
                live_sink(stream, text)
        if activity_sink is None:
            return
        with lock:
            offset = offsets.get(key, 0)
            sequence = sequences.get(key, 0) + 1
            # Offsets are UTF-8 byte offsets so Rust/TypeScript consumers can
            # replay Unicode output without disagreeing about code-unit size.
            byte_length = len(text.encode("utf-8"))
            offsets[key] = offset + byte_length
            sequences[key] = sequence
        with contextlib.suppress(Exception):
            activity_sink(
                "tool.output.delta",
                {
                    "tool": tool,
                    "tool_call_id": tool_call_id,
                    "stream": stream,
                    "delta": text,
                    "stream_seq": sequence,
                    "offset_start": offset,
                    "offset_end": offset + byte_length,
                },
            )

    return emit
