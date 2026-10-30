"""A desktop conversation, saved and reloaded every turn, resends each earlier
message exactly as it went the first time, reasoning included (F42 guard).

Phase 2 showed luna and the local Qwen stop caching right where the first
assistant turn starts. This rebuilds that conversation without any network:
a fresh runtime per turn, the history saved and restored, and continuations
shaped like the ones Responses (encrypted reasoning items) and Chat
Completions (`reasoning_content`) return.
"""

from __future__ import annotations

import logging
from itertools import pairwise

import pytest

from rinari.cli import agent_runtime
from rinari.models.router import _alias_map_for_request
from rinari.models.types import ModelResponse, ToolCall
from rinari.providers.adapters.openai_compatible import OpenAICompatibleAdapter
from rinari.providers.adapters.responses import OpenAIResponsesAdapter
from tests.unit.test_agent_loop import FakeModel
from tests.unit.test_tool_list_session import _session


def _responses_turn(n: int, *, call: bool) -> dict:
    items: list[dict] = [
        {"type": "reasoning", "id": f"rs_{n}", "summary": [], "encrypted_content": f"ENC{n}=="}
    ]
    if call:
        items.append(
            {
                "type": "function_call",
                "id": f"fc_{n}",
                "call_id": f"c{n}",
                "name": "fs_read",
                "arguments": '{"path":"a.txt"}',
                "status": "completed",
            }
        )
    else:
        items.append(
            {
                "type": "message",
                "id": f"msg_{n}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": f"answer {n}", "annotations": []}],
            }
        )
    return {"protocol": "responses", "items": items}


def _chat_turn(n: int, *, call: bool) -> dict:
    message: dict = {"role": "assistant", "content": f"step {n}" if call else f"answer {n}"}
    message["reasoning_content"] = f"thinking {n}"
    if call:
        message["tool_calls"] = [
            {
                "id": f"c{n}",
                "type": "function",
                "function": {"name": "fs_read", "arguments": '{"path": "a.txt"}'},
            }
        ]
    return {"protocol": "chat", "message": message}


def _script(shape) -> list[ModelResponse]:
    return [
        ModelResponse(
            content="Lo leo.",
            tool_calls=(ToolCall("c1", "fs.read", {"path": "a.txt"}),),
            continuation=shape(1, call=True),
        ),
        ModelResponse(
            content="Otra vez.",
            tool_calls=(ToolCall("c2", "fs.read", {"path": "a.txt"}),),
            continuation=shape(2, call=True),
        ),
        ModelResponse(content="answer 3", continuation=shape(3, call=False)),
        ModelResponse(content="answer 4", continuation=shape(4, call=False)),
    ]


def _chat(request):
    payload = OpenAICompatibleAdapter()._payload(
        request, stream=False, tool_aliases=_alias_map_for_request(None, request)
    )
    return payload["messages"]


def _responses(request):
    payload = OpenAIResponsesAdapter()._responses_payload(
        request, stream=False, tool_aliases=_alias_map_for_request(None, request)
    )
    return payload["input"]


@pytest.mark.parametrize(
    ("shape", "wire"),
    [(_responses_turn, _responses), (_chat_turn, _chat)],
    ids=["responses", "chat"],
)
def test_every_earlier_turn_is_resent_as_it_went(
    app_ctx, tmp_path, caplog, monkeypatch, shape, wire
) -> None:
    # The old trailing note, kept for one version as a way back.
    monkeypatch.setenv("RINARI_TURN_STATE", "trailing")
    services, record = _session(app_ctx, tmp_path)
    services.sessions.name_from_first_message = lambda *a, **k: None
    (tmp_path / "work" / "a.txt").write_text("uno\n", encoding="utf-8")
    model = FakeModel(scripted=_script(shape))
    with caplog.at_level(logging.WARNING, logger="rinari.cache"):
        for message in ("read a.txt twice", "and again?"):
            # A fresh runtime per turn, the history saved and restored, as on
            # the desktop.
            session = agent_runtime.build_agent_session(services, record, interactive=False)
            session.loop._provider = model
            agent_runtime.run_turn(session, message)
            session.end()
    assert len(model.requests) == 4
    for before, after in pairwise(model.requests):
        sent, resent = wire(before), wire(after)
        # The last element is the volatile note: everything else again.
        assert resent[: len(sent) - 1] == sent[:-1]
    # Every assistant turn of the conversation keeps its reasoning.
    last = wire(model.requests[-1])
    text = repr(last)
    for n in (1, 2, 3):
        assert (f"ENC{n}==" if shape is _responses_turn else f"thinking {n}") in text
    assert not [r for r in caplog.records if r.name == "rinari.cache"]


def test_the_guard_names_the_message_that_was_resent_differently(tmp_path, caplog) -> None:
    from dataclasses import replace

    from rinari.models.types import ChatMessage, ModelRequest
    from rinari.runtime.agent import _first_divergence, _message_digests

    one = ModelRequest(
        model="m",
        messages=(
            ChatMessage.system("rules"),
            ChatMessage.user("hi"),
            ChatMessage.assistant("", continuation={"protocol": "chat", "message": {"r": 1}}),
        ),
    )
    two = replace(
        one,
        messages=(
            *one.messages[:2],
            ChatMessage.assistant("", continuation=None),
            ChatMessage.user("next"),
        ),
    )
    assert _first_divergence(_message_digests(one), _message_digests(two)) == 1
    grown = replace(one, messages=(*one.messages, ChatMessage.user("next")))
    assert _first_divergence(_message_digests(one), _message_digests(grown)) is None


@pytest.mark.parametrize(
    ("shape", "wire"),
    [(_responses_turn, _responses), (_chat_turn, _chat)],
    ids=["responses", "chat"],
)
def test_with_the_state_kept_in_history_each_request_extends_the_last(
    app_ctx, tmp_path, monkeypatch, shape, wire
) -> None:
    """The turn state is kept in the conversation (F42): nothing ever comes off the end."""
    services, record = _session(app_ctx, tmp_path)
    services.sessions.name_from_first_message = lambda *a, **k: None
    (tmp_path / "work" / "a.txt").write_text("uno\n", encoding="utf-8")
    model = FakeModel(scripted=_script(shape))
    for message in ("read a.txt twice", "and again?"):
        session = agent_runtime.build_agent_session(services, record, interactive=False)
        session.loop._provider = model
        agent_runtime.run_turn(session, message)
        session.end()
    assert len(model.requests) == 4
    for before, after in pairwise(model.requests):
        sent, resent = wire(before), wire(after)
        assert resent[: len(sent)] == sent
    states = [
        m for m in model.requests[-1].messages if (m.origin or {}).get("source") == "turn-state"
    ]
    # Written when it changed, not once per request.
    assert 1 <= len(states) < len(model.requests)
    assert not any(m.is_turn_context for m in model.requests[-1].messages)


def _strip(value):
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def _blocks(content) -> list:
    """A text-only user turn is the same whether sent as text or as one block."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content)


def test_a_state_change_mid_turn_is_appended_and_anthropic_keeps_turns_alternating(
    app_ctx, tmp_path
) -> None:
    from dataclasses import replace

    from rinari.providers.adapters.anthropic import AnthropicAdapter

    services, record = _session(app_ctx, tmp_path)
    services.sessions.name_from_first_message = lambda *a, **k: None
    (tmp_path / "work" / "a.txt").write_text("uno\n", encoding="utf-8")
    model = FakeModel(scripted=_script(_chat_turn))
    session = agent_runtime.build_agent_session(services, record, interactive=False)
    original = model.invoke

    def invoke(request):
        response = original(request)
        if len(model.requests) == 1:
            # Something the harness tracks changes between two calls.
            base = session.context.assembler_base
            session.context.assembler_base = replace(base, task_state="step 2 of 3 is running")
        return response

    model.invoke = invoke
    session.loop._provider = model
    agent_runtime.run_turn(session, "read a.txt twice")
    session.end()
    states = [
        m for m in model.requests[-1].messages if (m.origin or {}).get("source") == "turn-state"
    ]
    assert len(states) == 2 and "step 2 of 3" in states[1].content
    payloads = [
        AnthropicAdapter()._payload(r, stream=False, tool_aliases=_alias_map_for_request(None, r))
        for r in model.requests
    ]
    for payload in payloads:
        roles = [m["role"] for m in payload["messages"]]
        assert all(a != b for a, b in pairwise(roles)), roles
    for before, after in pairwise(payloads):
        sent, resent = _strip(before["messages"]), _strip(after["messages"])
        assert resent[: len(sent) - 1] == sent[:-1]
        # The last user turn only gains blocks; what it had stays as it was.
        last, same = sent[-1], resent[len(sent) - 1]
        assert _blocks(same["content"])[: len(_blocks(last["content"]))] == _blocks(last["content"])


def test_runtime_notes_are_not_the_owners_words(app_ctx, tmp_path) -> None:
    from rinari.context.compact_state import explicit_goal, extract_from_history
    from rinari.models.types import ChatMessage

    note = ChatMessage.harness(
        "<turn-context>Nuevo objetivo: nada; must not</turn-context>", "turn-state"
    )
    history = [ChatMessage.user("arregla el login"), note]
    assert extract_from_history(tuple(history)).goal == "arregla el login"
    assert extract_from_history(tuple(history)).constraints == ()
    assert explicit_goal(history) == ""


def test_memory_never_captures_a_runtime_note() -> None:
    from types import SimpleNamespace

    from rinari.storage.records import SessionMessageRecord

    captured = []
    services = SimpleNamespace(
        memory=SimpleNamespace(capture_owner_message=lambda *args: captured.append(args[2]))
    )
    owner = SessionMessageRecord(id="m1", session_id="s", seq=1, role="user", content="soy Ana")
    note = SessionMessageRecord(
        id="m2",
        session_id="s",
        seq=2,
        role="user",
        content="<turn-context>mode: build</turn-context>",
        origin={"kind": "harness", "source": "turn-state"},
    )
    agent_runtime._capture_owner_messages(services, "s", [owner, note])
    assert captured == ["soy Ana"]


def test_a_cancelled_turn_leaves_its_note_and_the_next_one_reuses_it(app_ctx, tmp_path) -> None:
    from rinari.models.types import ModelResponse as Response
    from rinari.shared.errors import CancelledError

    services, record = _session(app_ctx, tmp_path)
    services.sessions.name_from_first_message = lambda *a, **k: None
    # Something for the turn state to say (the folder's files).
    (tmp_path / "work" / "a.txt").write_text("uno\n", encoding="utf-8")

    class Cancelling(FakeModel):
        def invoke(self, request):
            self.requests.append(request)
            raise CancelledError("stopped by the owner")

    def turn(model, message):
        session = agent_runtime.build_agent_session(services, record, interactive=False)
        session.loop._provider = model
        agent_runtime.run_turn(session, message)
        session.end()

    def kept():
        return [
            row
            for row in services.ctx.message_repo.list(record.id)
            if (row.origin or {}).get("source") == "turn-state"
        ]

    turn(Cancelling(scripted=[]), "Empieza por revisar el proyecto, por favor.")
    assert len(kept()) == 1
    turn(FakeModel(scripted=[Response(content="ok")]), "Sigue con lo que estabas haciendo.")
    assert len(kept()) == 1  # same state: no second copy
    # With the conversation forgotten (messages and events), the next turn
    # writes it again.
    for table in ("session_messages", "session_events"):
        services.ctx.db.execute(f"DELETE FROM {table} WHERE session_id = ?", (record.id,))
    turn(
        FakeModel(scripted=[Response(content="ok")]), "Empieza por revisar el proyecto, por favor."
    )
    assert len(kept()) == 1
