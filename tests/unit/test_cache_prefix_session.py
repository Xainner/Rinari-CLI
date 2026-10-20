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
def test_every_earlier_turn_is_resent_as_it_went(app_ctx, tmp_path, caplog, shape, wire) -> None:
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
