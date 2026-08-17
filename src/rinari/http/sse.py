"""Server-Sent Events parsing per the WHATWG SSE semantics (phase 5).

Line-oriented state machine: `data:` fields accumulate (joined with newlines),
`event:` names the event, `id:` tracks the last event id, `retry:` sets the
reconnect hint. A blank line dispatches the buffered event when it has data.
Comments (`:` prefix) are ignored.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

DEFAULT_EVENT_NAME = "message"


@dataclass(frozen=True, slots=True)
class SseEvent:
    event: str
    data: str
    last_event_id: str | None = None
    retry_ms: int | None = None


def parse_sse_events(
    lines: Iterable[str],
    *,
    max_events: int = 200,
) -> tuple[list[SseEvent], bool]:
    """Parse SSE lines; returns (events, stopped_early_at_max_events)."""
    max_events = max(1, int(max_events))
    events: list[SseEvent] = []
    event_name: str | None = None
    data_chunks: list[str] = []
    last_event_id: str | None = None
    retry_ms: int | None = None

    def dispatch() -> bool:
        nonlocal event_name, data_chunks
        if not data_chunks:
            event_name = None
            return False
        events.append(
            SseEvent(
                event=event_name or DEFAULT_EVENT_NAME,
                data="\n".join(data_chunks),
                last_event_id=last_event_id,
                retry_ms=retry_ms,
            )
        )
        event_name = None
        data_chunks = []
        return len(events) >= max_events

    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if dispatch():
                return events, True
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_chunks.append(value)
        elif field == "event" and not event_name:
            event_name = value
        elif field == "id" and value:
            last_event_id = value
        elif field == "retry" and value.isdigit():
            retry_ms = int(value)
    if dispatch():
        return events, len(events) >= max_events
    return events, False


__all__ = ["DEFAULT_EVENT_NAME", "SseEvent", "parse_sse_events"]
