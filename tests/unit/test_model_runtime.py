import json

import httpx
import pytest

from rinari.application.context import build_app_context
from rinari.models.router import ModelRouter
from rinari.models.types import (
    ChatMessage,
    ModelRequest,
    ProviderCapabilities,
    StopReason,
    ToolSchema,
    Usage,
)
from rinari.providers.adapters.anthropic import AnthropicAdapter
from rinari.providers.adapters.openai_compatible import OpenAICompatibleAdapter
from rinari.shared.clock import FakeClock
from rinari.shared.errors import InvalidUsageError, ProviderModelError
from rinari.shared.paths import ENV_HOME


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _request(**kwargs) -> ModelRequest:
    base = dict(
        model="model-x",
        messages=(ChatMessage.system("sys"), ChatMessage.user("hola")),
    )
    base.update(kwargs)
    return ModelRequest(**base)


# -- OpenAI-compatible: invoke ---------------------------------------------


def test_openai_invoke_normalizes_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "choices": [
                    {"message": {"role": "assistant", "content": "hola"}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 4},
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            },
        )

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    response = adapter.invoke(_request(), "sk-test", None)
    assert response.content == "hola"
    assert response.tool_calls == ()
    assert response.stop_reason is StopReason.END_TURN
    assert response.usage == Usage(
        input_tokens=10, output_tokens=20, cached_input_tokens=4, reasoning_tokens=3
    )
    assert response.usage.total_tokens == 30


def test_openai_invoke_tool_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "fs.read",
                                        "arguments": json.dumps({"path": "a.txt"}),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6},
            },
        )

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    response = adapter.invoke(_request(), "sk-test", None)
    assert response.has_tool_calls
    call = response.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "fs.read"
    assert call.arguments == {"path": "a.txt"}
    assert response.stop_reason is StopReason.TOOL_CALLS


def test_openai_invoke_sends_tools_and_options() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ]
            },
        )

    request = _request(
        tools=(
            ToolSchema(name="fs.read", description="read a file", parameters={"type": "object"}),
        ),
        temperature=0.2,
        max_tokens=100,
        reasoning_effort="low",
        json_response=True,
        messages=(
            ChatMessage.system("sys"),
            ChatMessage.assistant("yo pedí", tool_calls=()),
            ChatMessage.tool_result("call_9", "fs.read", "contenido"),
            ChatMessage.user("y ahora?"),
        ),
    )
    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    adapter.invoke(request, "sk-test", None)

    body = json.loads(seen[0].content)
    assert body["stream"] is False
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 100
    assert body["reasoning_effort"] == "low"
    assert body["response_format"] == {"type": "json_object"}
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "fs.read",
                "description": "read a file",
                "parameters": {"type": "object"},
            },
        }
    ]
    tool_message = body["messages"][2]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_9"


def test_openai_invoke_auth_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    with pytest.raises(ProviderModelError) as excinfo:
        adapter.invoke(_request(), "sk-bad", None)
    assert "Authentication failed" in excinfo.value.message


def test_openai_invoke_provider_error_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "model exploded"}})

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    with pytest.raises(ProviderModelError) as excinfo:
        adapter.invoke(_request(), "sk-x", None)
    assert "model exploded" in excinfo.value.message


def test_openai_invoke_malformed_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"weird": True})

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    with pytest.raises(ProviderModelError):
        adapter.invoke(_request(), "sk-x", None)


# -- OpenAI-compatible: streaming --------------------------------------------


def test_openai_stream_text() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"ho"}}]}\n'
        'data: {"choices":[{"delta":{"content":"la"}}]}\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
        "data: [DONE]\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=body.encode())

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    deltas: list[str] = []
    response = adapter.invoke_stream(_request(), "sk-x", None, deltas.append)
    assert response.content == "hola"
    assert deltas == ["ho", "la"]
    assert response.stop_reason is StopReason.END_TURN
    assert response.usage.input_tokens is None


def test_openai_stream_tool_calls() -> None:
    body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"s","arguments":""}}]}}]}\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"name":"h","arguments":"{\\"p\\":"}}]}}]}\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"\\"x\\"}"}}]}}]}\n'
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n'
        "data: [DONE]\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode())

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    response = adapter.invoke_stream(_request(), "sk-x", None, lambda d: None)
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "sh"
    assert response.tool_calls[0].arguments == {"p": "x"}
    assert response.stop_reason is StopReason.TOOL_CALLS


def test_openai_stream_auth_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    with pytest.raises(ProviderModelError):
        adapter.invoke_stream(_request(), "sk-bad", None, lambda d: None)


# -- Anthropic ---------------------------------------------------------------


def test_anthropic_invoke_normalizes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "hola"}, {"type": "text", "text": " mundo"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 8, "output_tokens": 12, "cache_read_input_tokens": 2},
            },
        )

    adapter = AnthropicAdapter(client=_client(handler))
    response = adapter.invoke(_request(), "key", None)
    assert response.content == "hola mundo"
    assert response.stop_reason is StopReason.END_TURN
    assert response.usage.input_tokens == 8
    assert response.usage.output_tokens == 12
    assert response.usage.cached_input_tokens == 2


def test_anthropic_invoke_tool_use() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "voy a leer"},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "fs.read",
                        "input": {"path": "b.txt"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        )

    adapter = AnthropicAdapter(client=_client(handler))
    response = adapter.invoke(_request(), "key", None)
    assert response.tool_calls[0].name == "fs.read"
    assert response.tool_calls[0].arguments == {"path": "b.txt"}
    assert response.stop_reason is StopReason.TOOL_CALLS


def test_anthropic_converts_messages() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"content": [], "stop_reason": "end_turn", "usage": {}})

    request = _request(
        messages=(
            ChatMessage.system("constitución"),
            ChatMessage.user("hola"),
            ChatMessage.assistant("ok", tool_calls=()),
            ChatMessage.user("otra"),
            ChatMessage.tool_result("tu_1", "fs.read", "archivo"),
            ChatMessage.user("listo"),
        )
    )
    adapter = AnthropicAdapter(client=_client(handler))
    adapter.invoke(request, "key", None)

    body = json.loads(seen[0].content)
    assert body["system"] == "constitución"
    assert body["max_tokens"] == 8192
    assert body["messages"][0] == {"role": "user", "content": "hola"}
    assert body["messages"][1] == {"role": "assistant", "content": "ok"}
    # adjacent user messages merged, tool_result becomes a user block
    assert body["messages"][2] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "otra"},
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "archivo"},
            {"type": "text", "text": "listo"},
        ],
    }


def test_anthropic_tools_in_payload() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"content": [], "stop_reason": "end_turn", "usage": {}})

    request = _request(
        tools=(ToolSchema(name="fs.read", description="lee", parameters={"type": "object"}),)
    )
    adapter = AnthropicAdapter(client=_client(handler))
    adapter.invoke(request, "key", None)
    body = json.loads(seen[0].content)
    assert body["tools"] == [
        {"name": "fs.read", "description": "lee", "input_schema": {"type": "object"}}
    ]


def test_anthropic_stream() -> None:
    body = (
        'event: message_start\ndata: {"type":"message_start",'
        '"message":{"usage":{"input_tokens":5,"cache_read_input_tokens":1}}}\n'
        'event: content_block_start\ndata: {"type":"content_block_start",'
        '"index":0,"content_block":{"type":"text","text":""}}\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta",'
        '"index":0,"delta":{"type":"text_delta","text":"ho"}}\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta",'
        '"index":0,"delta":{"type":"text_delta","text":"la"}}\n'
        'event: content_block_start\ndata: {"type":"content_block_start",'
        '"index":1,"content_block":{"type":"tool_use","id":"tu_1","name":"fs.read","input":{}}}\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta",'
        '"index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"p\\":"}}\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta",'
        '"index":1,"delta":{"type":"input_json_delta","partial_json":"\\"x\\"}"}}\n'
        'event: message_delta\ndata: {"type":"message_delta",'
        '"delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":7}}\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode())

    adapter = AnthropicAdapter(client=_client(handler))
    deltas: list[str] = []
    response = adapter.invoke_stream(_request(), "key", None, deltas.append)
    assert response.content == "hola"
    assert deltas == ["ho", "la"]
    assert response.tool_calls[0].name == "fs.read"
    assert response.tool_calls[0].arguments == {"p": "x"}
    assert response.stop_reason is StopReason.TOOL_CALLS
    assert response.usage.input_tokens == 5
    assert response.usage.output_tokens == 7
    assert response.usage.cached_input_tokens == 1


# -- capabilities --------------------------------------------------------------


def test_capabilities() -> None:
    openai = OpenAICompatibleAdapter("https://api.test/v1")
    assert openai.capabilities() == ProviderCapabilities(
        streaming=True, tool_calls=True, structured_output=True, reasoning_effort=True
    )
    anthropic = AnthropicAdapter()
    assert anthropic.capabilities() == ProviderCapabilities(
        streaming=True, tool_calls=True, structured_output=False, reasoning_effort=False
    )


# -- router ---------------------------------------------------------------------


def _app_and_services(handler, monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv(ENV_HOME, str(home))
    ctx = build_app_context(home=str(home), clock=FakeClock(start=1_700_000_000.0, step=1.0))
    from rinari.application.services import build_services

    client = _client(handler)
    services = build_services(ctx, http_client=client)
    return ctx, services


def test_router_uses_provider_model_id_and_secret(monkeypatch, tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        provider = services.providers.add(_add_input(services, "openai", "https://api.test/v1"))
        model = services.models.add(provider.alias, "gpt-real", "principal")
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        response = router.invoke(provider, model.id, _request(model="ignored-name"))
        assert response.content == "ok"
        body = json.loads(seen[-1].content)
        assert body["model"] == "gpt-real"
        assert seen[-1].headers["Authorization"] == "Bearer sk-test-key"
    finally:
        ctx.close()


def _add_input(services, alias: str, endpoint: str):
    from rinari.application.provider_service import AddProviderInput

    return AddProviderInput(
        alias=alias,
        provider_type="openai",
        auth_method="api-key",
        endpoint=endpoint,
        secret="sk-test-key",
    )


def test_router_model_mismatch(monkeypatch, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        a = services.providers.add(_add_input(services, "prov-a", "https://a.test/v1"))
        b = services.providers.add(_add_input(services, "prov-b", "https://b.test/v1"))
        model = services.models.add(a.alias, "m-a", "ma")
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        with pytest.raises(InvalidUsageError):
            router.invoke(b, model.id, _request())
    finally:
        ctx.close()


def test_router_without_model(monkeypatch, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        a = services.providers.add(_add_input(services, "prov-a", "https://a.test/v1"))
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        with pytest.raises(InvalidUsageError):
            router.invoke(a, None, _request())
    finally:
        ctx.close()
