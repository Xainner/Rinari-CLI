"""Unchanged re-reads answer with a pointer only while the model still sees the text."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from rinari.context.settle import BLOCK_ROUNDS, KEEP_ROUNDS, settle_old_observations
from rinari.models.types import ChatMessage, ModelResponse, StopReason, ToolCall
from rinari.runtime.agent import AgentLoop
from rinari.tools.native.fs import fs_read, fs_read_lines
from rinari.tools.read_cache import for_agent, numbered_rows
from tests.unit.test_agent_loop import FakeModel, env  # noqa: F401 (fixture)
from tests.unit.test_tool_runtime import _ctx

BODY = "".join(f"line {n}: {'x' * 40}\n" for n in range(1, 61))  # ~3 KB


@dataclass
class _Conversation:
    """The slice of AgentContext the cache reads."""

    history: list = field(default_factory=list)
    compact_revision: int = 0
    read_cache: object = None


_HANDLERS = {"fs.read": fs_read, "fs.read_lines": fs_read_lines}


def _read(conversation, base, call_id, tool="fs.read", **args):
    """Run one read the way AgentLoop does and keep its observation in history."""
    ctx = replace(base, reads=for_agent(conversation), tool_call_id=call_id)
    result = _HANDLERS[tool](args, ctx)
    conversation.history.append(
        ChatMessage.assistant("", (ToolCall(id=call_id, name=tool, arguments=args),))
    )
    conversation.history.append(ChatMessage.tool_result(call_id, tool, result.to_model_text(tool)))
    return result


def _other_rounds(conversation, count):
    for index in range(count):
        call_id = f"other{len(conversation.history)}_{index}"
        conversation.history.append(
            ChatMessage.assistant("", (ToolCall(id=call_id, name="fs.list", arguments={}),))
        )
        conversation.history.append(ChatMessage.tool_result(call_id, "fs.list", "{}"))


def _setup(tmp_path, text=BODY):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "notes.txt").write_bytes(text.encode("utf-8"))
    return _ctx(tmp_path, root), _Conversation()


def test_rereading_an_unchanged_file_returns_a_pointer_not_the_text(tmp_path):
    base, conversation = _setup(tmp_path)
    first = _read(conversation, base, "c1", path="notes.txt")
    assert first.data["text"] == BODY

    again = _read(conversation, base, "c2", path="notes.txt")

    assert again.ok and again.data["unchanged"] is True
    assert "text" not in again.data
    assert again.data["sha256"] == first.data["sha256"]
    assert "fresh=true" in again.data["note"] and "1 call(s) ago" in again.data["note"]
    assert len(again.to_model_text("fs.read")) < 600


def test_a_changed_file_is_read_in_full(tmp_path):
    base, conversation = _setup(tmp_path)
    _read(conversation, base, "c1", path="notes.txt")
    (tmp_path / "proj" / "notes.txt").write_text(BODY + "one more\n", encoding="utf-8", newline="")

    again = _read(conversation, base, "c2", path="notes.txt")

    assert again.data["text"].endswith("one more\n")
    assert "unchanged" not in again.data


def test_fresh_forces_the_full_text(tmp_path):
    base, conversation = _setup(tmp_path)
    _read(conversation, base, "c1", path="notes.txt")

    again = _read(conversation, base, "c2", path="notes.txt", fresh=True)

    assert again.data["text"] == BODY


def test_small_files_are_always_sent_whole(tmp_path):
    base, conversation = _setup(tmp_path, text="short\n")
    _read(conversation, base, "c1", path="notes.txt")

    assert _read(conversation, base, "c2", path="notes.txt").data["text"] == "short\n"


def test_a_read_settled_out_of_the_request_is_not_pointed_at(tmp_path):
    base, conversation = _setup(tmp_path)
    _read(conversation, base, "c1", path="notes.txt")
    # Enough later rounds for settle.py to replace the first result with a note.
    _other_rounds(conversation, KEEP_ROUNDS + BLOCK_ROUNDS)
    settled = settle_old_observations(conversation.history)
    assert "left out" in next(m.content for m in settled if m.tool_call_id == "c1")

    again = _read(conversation, base, "c2", path="notes.txt")

    assert again.data["text"] == BODY


def test_a_read_still_inside_the_kept_rounds_is_pointed_at(tmp_path):
    base, conversation = _setup(tmp_path)
    _read(conversation, base, "c1", path="notes.txt")
    _other_rounds(conversation, KEEP_ROUNDS - 2)

    again = _read(conversation, base, "c2", path="notes.txt")

    assert again.data["unchanged"] is True
    assert f"{KEEP_ROUNDS - 1} call(s) ago" in again.data["note"]


def test_compaction_clears_the_cache(tmp_path):
    base, conversation = _setup(tmp_path)
    _read(conversation, base, "c1", path="notes.txt")
    conversation.compact_revision += 1

    assert _read(conversation, base, "c2", path="notes.txt").data["text"] == BODY


def test_a_result_that_did_not_reach_the_model_whole_is_not_pointed_at(tmp_path):
    base, conversation = _setup(tmp_path)
    _read(conversation, base, "c1", path="notes.txt")
    # The round projection spilled it: the history holds a preview, not the text.
    conversation.history[-1] = replace(conversation.history[-1], content="[preview] line 1 ...")

    assert _read(conversation, base, "c2", path="notes.txt").data["text"] == BODY


def test_a_read_dropped_from_the_history_is_not_pointed_at(tmp_path):
    base, conversation = _setup(tmp_path)
    _read(conversation, base, "c1", path="notes.txt")
    _other_rounds(conversation, 2)
    # Messages trimmed from memory: the earlier result is gone from the request.
    del conversation.history[:2]

    assert _read(conversation, base, "c2", path="notes.txt").data["text"] == BODY


def test_a_line_range_inside_an_earlier_range_is_pointed_at(tmp_path):
    base, conversation = _setup(tmp_path)
    _read(conversation, base, "c1", tool="fs.read_lines", path="notes.txt", start=1, end=50)

    inside = _read(
        conversation, base, "c2", tool="fs.read_lines", path="notes.txt", start=5, end=40
    )
    beyond = _read(
        conversation, base, "c3", tool="fs.read_lines", path="notes.txt", start=30, end=60
    )

    assert inside.data["unchanged"] is True and "text" not in inside.data
    assert inside.data["start_line"] == 5 and inside.data["end_line"] == 40
    assert beyond.data["text"].startswith("30| line 30")


def test_a_line_range_of_a_file_read_whole_is_pointed_at(tmp_path):
    base, conversation = _setup(tmp_path)
    _read(conversation, base, "c1", path="notes.txt")

    lines = _read(conversation, base, "c2", tool="fs.read_lines", path="notes.txt", start=2, end=30)

    assert lines.data["unchanged"] is True
    assert "fs.read" in lines.data["note"]


def test_numbered_rows_match_fs_read_lines_for_mixed_line_endings(tmp_path):
    endings = ("\n", "\r\n", "\r")
    text = "".join(f"row {n} {'y' * 30}" + endings[n % 3] for n in range(1, 40))
    base, _ = _setup(tmp_path, text=text)
    rows = fs_read_lines({"path": "notes.txt", "start": 3, "end": 21}, base).data["text"]

    assert numbered_rows(text, 3, 21) == rows


def test_batched_reads_are_remembered_per_file(tmp_path):
    base, conversation = _setup(tmp_path)
    (tmp_path / "proj" / "other.txt").write_text(BODY.upper(), encoding="utf-8", newline="")
    _read(conversation, base, "c1", paths=["notes.txt", "other.txt"])

    again = _read(conversation, base, "c2", path="other.txt")

    assert again.data["unchanged"] is True


def test_the_agent_loop_dedupes_a_reread_in_the_same_turn(env):  # noqa: F811
    (env["root"] / "notes.txt").write_text(BODY, encoding="utf-8", newline="")

    def read(call_id):
        call = ToolCall(id=call_id, name="fs.read", arguments={"path": "notes.txt"})
        return ModelResponse(content="", tool_calls=(call,), stop_reason=StopReason.TOOL_CALLS)

    model = FakeModel(scripted=[read("t1"), read("t2"), ModelResponse(content="done")])
    AgentLoop(model, env["runtime"], env["assembler"]).turn(env["ctx"], "read it twice")

    results = [m for m in env["ctx"].history if m.role == "tool"]
    first, second = (json.loads(m.content)["data"] for m in results)
    assert first["text"] == BODY
    assert second["unchanged"] is True and "text" not in second
