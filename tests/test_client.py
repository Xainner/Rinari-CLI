"""Tests para el cliente OpenAI-compatible con streaming SSE."""

import json

import httpx
import pytest

from rinari.client import LLMError, LLMClient

BASE = "http://test.local/v1"
MODEL = "qwen3.6-27b"


def sse_chunk(delta_content: str | None = None, finish: bool = False,
              tool_calls: list | None = None) -> str:
    """Construye un chunk SSE de chat.completion.chunk."""
    delta: dict = {}
    if delta_content is not None:
        delta["content"] = delta_content
    if tool_calls:
        delta["tool_calls"] = tool_calls
    choice = {"index": 0, "delta": delta}
    if finish:
        choice["finish_reason"] = "stop"
    chunk = {"id": "x", "object": "chat.completion.chunk", "choices": [choice]}
    return f"data: {json.dumps(chunk)}\n\n"


def mock_transport(handler):
    return httpx.MockTransport(handler)


def make_client(transport) -> LLMClient:
    return LLMClient(
        base_url=BASE,
        api_key=None,
        model=MODEL,
        transport=transport,
    )


def test_streaming_parses_sse_deltas():
    def handler(request: httpx.Request) -> httpx.Response:
        body = sse_chunk("Hola") + sse_chunk(" mundo") + sse_chunk("", finish=True) + "data: [DONE]\n\n"
        return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})

    client = make_client(mock_transport(handler))
    deltas = list(client.chat_stream([{"role": "user", "content": "hi"}]))
    assert deltas == ["Hola", " mundo"]


def test_streaming_sends_correct_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, content=sse_chunk("ok") + "data: [DONE]\n\n")

    client = make_client(mock_transport(handler))
    list(client.chat_stream([{"role": "user", "content": "hola"}], temperature=0.3))
    body = captured["body"]
    assert body["model"] == MODEL
    assert body["stream"] is True
    assert body["temperature"] == 0.3
    assert body["messages"][0]["content"] == "hola"


def test_streaming_sends_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, content=sse_chunk("ok") + "data: [DONE]\n\n")

    client = LLMClient(
        base_url=BASE, api_key="sk-secret", model=MODEL,
        transport=mock_transport(handler),
    )
    list(client.chat_stream([{"role": "user", "content": "hola"}]))
    assert captured["auth"] == "Bearer sk-secret"


def test_streaming_without_key_no_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, content=sse_chunk("ok") + "data: [DONE]\n\n")

    client = make_client(mock_transport(handler))
    list(client.chat_stream([{"role": "user", "content": "hola"}]))
    assert captured["auth"] is None


def test_http_error_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content='{"error": {"message": "bad key"}}')

    client = make_client(mock_transport(handler))
    with pytest.raises(LLMError, match="401"):
        list(client.chat_stream([{"role": "user", "content": "hi"}]))


def test_non_stream_returns_full_content():
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"id": "x", "choices": [{"message": {"role": "assistant", "content": "respuesta completa"}}]}
        return httpx.Response(200, json=body)

    client = make_client(mock_transport(handler))
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result == "respuesta completa"


def test_non_stream_uses_stream_false():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    client = make_client(mock_transport(handler))
    client.chat([{"role": "user", "content": "hi"}])
    assert captured["body"]["stream"] is False


def test_network_error_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = make_client(mock_transport(handler))
    with pytest.raises(LLMError, match="conectar|connect|refused|error"):
        list(client.chat_stream([{"role": "user", "content": "hi"}]))


def test_stream_with_tool_calls_parses_delta():
    """El stream puede traer tool_calls en el delta (modo agente)."""
    tool_delta = [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "run_command", "arguments": '{"cmd": "ls"}'},
    }]

    def handler(request: httpx.Request) -> httpx.Response:
        body = sse_chunk(tool_calls=tool_delta) + sse_chunk("", finish=True) + "data: [DONE]\n\n"
        return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})

    client = make_client(mock_transport(handler))
    events = list(client.chat_stream([{"role": "user", "content": "hi"}]))
    # El primer elemento es un dict con tool_calls
    assert events[0]["tool_calls"][0]["function"]["name"] == "run_command"


def test_list_models():
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"data": [{"id": "qwen3.6-27b"}, {"id": "otro"}]}
        return httpx.Response(200, json=body)

    client = make_client(mock_transport(handler))
    models = client.list_models()
    assert models == ["qwen3.6-27b", "otro"]
