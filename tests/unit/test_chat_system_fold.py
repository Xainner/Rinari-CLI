"""A chat template that refuses the system role (Gemma 2 on llama.cpp or vLLM).

The server answers a plain 400/500 to any request with a system message. The
adapter tries the same request once with the system folded into the first
user message and, when that works, sends it that way to that server and model
from then on. Nothing is read from the error's text.
"""

from __future__ import annotations

import json

import httpx
import pytest

from rinari.models.types import ChatMessage, ModelRequest
from rinari.providers.adapters import openai_compatible
from rinari.providers.adapters.openai_compatible import OpenAICompatibleAdapter
from rinari.shared.errors import ProviderModelError

ENDPOINT = "http://127.0.0.1:8080/v1"


@pytest.fixture(autouse=True)
def _fresh_memory(monkeypatch):
    monkeypatch.setattr(openai_compatible, "_SYSTEM_FOLD", {})


def _request(model: str = "gemma-2") -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=(
            ChatMessage.system("You are Rinari."),
            ChatMessage.user("Hola"),
            ChatMessage.harness("Local time now: 2026-10-21 18:30 (Wednesday).", "clock"),
        ),
    )


def _answer(stream: bool) -> httpx.Response:
    if not stream:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "¡Hola!"}, "finish_reason": "stop"}
                ]
            },
        )
    chunks = [
        {"choices": [{"delta": {"content": "¡Hola!"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _server(sent: list, *, refuse_status: int = 500, folding_helps: bool = True):
    """A server whose template refuses the system role, like Gemma 2's."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body["messages"])
        roles = [m["role"] for m in body["messages"]]
        if "system" in roles or not folding_helps:
            # llama.cpp's shape for a template exception; the text is never read.
            error = {"code": refuse_status, "message": "template error", "type": "server_error"}
            return httpx.Response(refuse_status, json={"error": error})
        return _answer(body.get("stream", False))

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status", [400, 500])
def test_a_refused_system_role_is_folded_once_and_remembered(stream, status) -> None:
    sent: list = []
    adapter = OpenAICompatibleAdapter(client=_server(sent, refuse_status=status))

    def call():
        if stream:
            return adapter.invoke_stream(_request(), None, ENDPOINT, lambda text: None)
        return adapter.invoke(_request(), None, ENDPOINT)

    assert call().content == "¡Hola!"
    assert [m["role"] for m in sent[0]] == ["system", "user"]
    assert [m["role"] for m in sent[1]] == ["user"]
    assert sent[1][0]["content"].startswith("You are Rinari.\n\nHola\n\nLocal time now:")
    # Remembered for this server and model: straight to the folded form.
    assert call().content == "¡Hola!"
    assert len(sent) == 3 and [m["role"] for m in sent[2]] == ["user"]


def test_a_server_that_accepts_the_system_role_is_untouched() -> None:
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["messages"])
        return _answer(False)

    adapter = OpenAICompatibleAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    adapter.invoke(_request(), None, ENDPOINT)
    assert len(sent) == 1 and sent[0][0]["role"] == "system"


def test_when_folding_does_not_help_the_first_error_stands_and_is_not_retried() -> None:
    sent: list = []
    adapter = OpenAICompatibleAdapter(client=_server(sent, folding_helps=False))
    with pytest.raises(ProviderModelError) as first:
        adapter.invoke(_request(), None, ENDPOINT)
    assert len(sent) == 2 and first.value.details["http_status"] == 500
    with pytest.raises(ProviderModelError):
        adapter.invoke(_request(), None, ENDPOINT)
    assert len(sent) == 3  # tried once, never again for this server and model


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (429, {"code": "rate_limit_exceeded", "message": "slow down"}),
        (400, {"code": "context_length_exceeded", "message": "too long"}),
        (503, {"type": "server_error", "message": "busy"}),
    ],
)
def test_limits_context_and_overload_are_never_retried_folded(status, error) -> None:
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["messages"])
        return httpx.Response(status, json={"error": error})

    adapter = OpenAICompatibleAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderModelError):
        adapter.invoke(_request(), None, ENDPOINT)
    assert len(sent) == 1


def test_a_tool_step_without_reasoning_still_carries_the_field() -> None:
    """DeepSeek's thinking mode: a tool step that returned no reasoning, sent back
    without `reasoning_content`, made the next request fail with a 400."""

    def step(reasoning):
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
            ],
        }
        if reasoning is not None:
            message["reasoning_content"] = reasoning
        return ChatMessage(
            role="assistant", content="", continuation={"protocol": "chat", "message": message}
        )

    request = ModelRequest(
        model="m",
        messages=(
            ChatMessage.user("hola"),
            step("pienso"),
            ChatMessage.tool_result("c1", "t", "ok"),
            step(None),
        ),
    )
    items = OpenAICompatibleAdapter()._payload(request, stream=True)["messages"]
    assistants = [m for m in items if m["role"] == "assistant"]
    assert assistants[0]["reasoning_content"] == "pienso"
    assert assistants[1]["reasoning_content"] == ""
    # A conversation that never carried it is left alone.
    plain = ModelRequest(model="m", messages=(ChatMessage.user("hola"), step(None)))
    items = OpenAICompatibleAdapter()._payload(plain, stream=True)["messages"]
    assert "reasoning_content" not in items[-1]
