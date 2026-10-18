"""A model writing a long tool call shows signs of life (plan 0.4.0 v2,
V2-15, F26): DeepSeek spent 5 min 13 s writing whole files inside fs.write
arguments with no event at all."""

from __future__ import annotations

import time

from rinari.providers.adapters.openai_compatible import _report_tool_input
from rinari.providers.adapters.responses import _ResponsesStreamAccumulator
from rinari.runtime.agent import ToolInputProgress


def test_progress_is_throttled_and_names_the_file() -> None:
    events = []
    progress = ToolInputProgress(lambda event, payload: events.append((event, payload)), "model_3")
    progress("fs.write", r'{"path": "calc\\parser.py", "content": "')  # JSON-escaped
    progress("fs.write", "x" * 1000)
    assert events == []  # small calls stay quiet
    progress("fs.write", "x" * 1500)
    progress("fs.write", "x" * 1500)  # within the same second
    assert len(events) == 1
    event, payload = events[0]
    assert event == "model.tool_input"
    assert payload["tool"] == "fs.write" and payload["path"] == r"calc\parser.py"
    assert payload["model_call_id"] == "model_3" and payload["chars"] > 2000
    progress._last = time.monotonic() - 2
    progress("fs.write", "y")
    assert len(events) == 2 and events[1][1]["chars"] > events[0][1]["chars"]


def test_chat_completions_deltas_reach_the_observer_with_their_tool_name() -> None:
    seen, names = [], {}
    _report_tool_input(
        lambda name, piece: seen.append((name, piece)),
        names,
        [{"index": 0, "function": {"name": "fs.write", "arguments": '{"pa'}}],
    )
    _report_tool_input(
        lambda name, piece: seen.append((name, piece)),
        names,
        [{"index": 0, "function": {"arguments": 'th": "a"'}}],
    )
    assert seen == [("fs.write", '{"pa'), ("fs.write", 'th": "a"')]


def test_responses_argument_deltas_reach_the_observer() -> None:
    seen = []
    acc = _ResponsesStreamAccumulator(lambda name, piece: seen.append((name, piece)))
    acc.update(
        {
            "type": "response.output_item.added",
            "item": {"type": "function_call", "id": "fc_1", "name": "fs.write"},
        },
        lambda _d: None,
    )
    acc.update(
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"p'},
        lambda _d: None,
    )
    assert seen == [("fs.write", '{"p')]
