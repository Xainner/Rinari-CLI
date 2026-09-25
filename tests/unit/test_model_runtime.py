import json

import httpx
import pytest

from rinari.application.context import build_app_context
from rinari.models.router import ModelRouter, _stream_read_timeout_s
from rinari.models.types import (
    ChatMessage,
    ModelRequest,
    ProviderCapabilities,
    StopReason,
    ToolCall,
    ToolSchema,
    Usage,
)
from rinari.providers.adapters.anthropic import AnthropicAdapter, _anthropic_input_schema
from rinari.providers.adapters.openai_compatible import OpenAICompatibleAdapter
from rinari.providers.errors import ProviderErrorCode
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


def test_stream_read_timeout_resolution_is_bounded_and_model_specific(monkeypatch) -> None:
    assert _stream_read_timeout_s({}, {}) == 30.0
    assert _stream_read_timeout_s({"stream_read_timeout_s": 90}, {}) == 90.0
    assert (
        _stream_read_timeout_s({"stream_read_timeout_s": 90}, {"stream_read_timeout_s": 120})
        == 120.0
    )

    monkeypatch.setenv("RINARI_MODEL_STREAM_READ_TIMEOUT_SECONDS", "45")
    assert _stream_read_timeout_s({}, {}) == 45.0
    for value in (0, 601, "bad", True):
        with pytest.raises(InvalidUsageError):
            _stream_read_timeout_s({"stream_read_timeout_s": value}, {})


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


# -- OpenCode session affinity header ----------------------------------------

OPENCODE_GO = "https://opencode.ai/zen/go/v1"


def _chat_ok() -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": "hola"}, "finish_reason": "stop"}],
    }


def test_openai_invoke_sends_opencode_session_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_chat_ok())

    adapter = OpenAICompatibleAdapter(OPENCODE_GO, client=_client(handler))
    response = adapter.invoke(_request(session_id="ses_123"), "sk-test", None)
    assert response.content == "hola"
    assert seen[0].headers["x-opencode-session"] == "ses_123"


def test_openai_stream_sends_opencode_session_header() -> None:
    seen: list[httpx.Request] = []
    body = 'data: {"choices":[{"delta":{"content":"hola"},"finish_reason":"stop"}]}\ndata: [DONE]\n'

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=body.encode())

    adapter = OpenAICompatibleAdapter(OPENCODE_GO, client=_client(handler))
    adapter.invoke_stream(_request(session_id="ses_123"), "sk-test", None, lambda d: None)
    assert seen[0].headers["x-opencode-session"] == "ses_123"


def test_openai_omits_session_header_off_vendor() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_chat_ok())

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    adapter.invoke(_request(session_id="ses_123"), "sk-test", None)
    assert "x-opencode-session" not in seen[0].headers


def test_openai_omits_session_header_without_session() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_chat_ok())

    adapter = OpenAICompatibleAdapter(OPENCODE_GO, client=_client(handler))
    adapter.invoke(_request(), "sk-test", None)
    assert "x-opencode-session" not in seen[0].headers


# -- Anthropic ---------------------------------------------------------------


def test_anthropic_stream_preserves_provider_error_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"request-id": "req_anthropic_1"},
            stream=httpx.ByteStream(
                b'{"type":"error","error":{"type":"invalid_request_error",'
                b'"message":"tools.4.input_schema: unsupported keyword"}}'
            ),
        )

    adapter = AnthropicAdapter(client=_client(handler))
    with pytest.raises(ProviderModelError) as excinfo:
        adapter.invoke_stream(_request(), "key", None, lambda _delta: None)

    error = excinfo.value
    assert "tools.4.input_schema: unsupported keyword" in error.message
    assert error.error_code == ProviderErrorCode.INVALID_TOOL_SCHEMA
    assert error.details["request_id"] == "req_anthropic_1"
    assert error.details["provider_error_code"] == "INVALID_TOOL_SCHEMA"


def test_anthropic_invoke_sends_opencode_session_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "hola"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    adapter = AnthropicAdapter(client=_client(handler))
    response = adapter.invoke(
        _request(session_id="ses_123"), "key", "https://opencode.ai/zen/go/v1"
    )
    assert response.content == "hola"
    assert seen[0].headers["x-opencode-session"] == "ses_123"


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
    # F3: the adapter alone sanitize-falls-back (invoked without the router's
    # alias map); dotted names must never reach the official wire.
    assert body["tools"] == [
        {"name": "fs_read", "description": "lee", "input_schema": {"type": "object"}}
    ]


@pytest.mark.parametrize("keyword", ["oneOf", "anyOf", "allOf"])
def test_anthropic_projects_unsupported_root_schema_combinators(keyword: str) -> None:
    original = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "paths": {"type": "array"}},
        keyword: [{"required": ["path"]}, {"required": ["paths"]}],
    }

    wire = _anthropic_input_schema(original)

    assert keyword not in wire
    assert wire["type"] == "object"
    assert "path" in wire["description"] and "paths" in wire["description"]
    assert keyword in original, "the runtime validation schema must remain unchanged"


def test_anthropic_payload_projects_root_combinator_but_keeps_runtime_schema() -> None:
    seen: list[httpx.Request] = []
    schema = {
        "type": "object",
        "properties": {"agent_id": {"type": "string"}, "agent_ids": {"type": "array"}},
        "oneOf": [{"required": ["agent_id"]}, {"required": ["agent_ids"]}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"content": [], "stop_reason": "end_turn", "usage": {}})

    request = _request(
        tools=(ToolSchema(name="agent.wait", description="wait", parameters=schema),)
    )
    AnthropicAdapter(client=_client(handler)).invoke(request, "key", None)
    wire_schema = json.loads(seen[0].content)["tools"][0]["input_schema"]

    assert "oneOf" not in wire_schema
    assert "exactly one" in wire_schema["description"].lower()
    assert "oneOf" in schema


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


def test_openai_marks_malformed_tool_arguments() -> None:
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
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "fs.read", "arguments": '{"path": '},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        )

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    response = adapter.invoke(_request(), "sk-test", None)
    (call,) = response.tool_calls
    assert call.arguments == {}
    assert call.arguments_invalid is True
    assert call.raw_arguments == '{"path": '
    assert response.stop_reason is StopReason.TOOL_CALLS


def test_capabilities() -> None:
    openai = OpenAICompatibleAdapter("https://api.test/v1")
    assert openai.capabilities() == ProviderCapabilities(
        streaming=True, tool_calls=True, structured_output=True, reasoning_effort=True
    )
    anthropic = AnthropicAdapter()
    assert anthropic.capabilities() == ProviderCapabilities(
        streaming=True, tool_calls=True, structured_output=False, reasoning_effort=False
    )


# -- vendor tool-name aliasing -------------------------------------------------

OPENCODE_GO_URL = "https://opencode.ai/zen/go/v1"


def test_sanitize_tool_name() -> None:
    from rinari.providers.adapters.http import sanitize_tool_name

    assert sanitize_tool_name("fs.read") == "fs_read"
    assert sanitize_tool_name("fs.read_lines") == "fs_read_lines"
    assert sanitize_tool_name("plain") == "plain"
    assert sanitize_tool_name("a b-c_d") == "a_b-c_d"


def test_native_tool_names_sanitize_injectively() -> None:
    from rinari.providers.adapters.http import sanitize_tool_name
    from rinari.tools.native import all_native_tools

    names = [t.name for t in all_native_tools()]
    assert len({sanitize_tool_name(n) for n in names}) == len(names)


def _add_custom_input(alias: str, endpoint: str):
    from rinari.application.provider_service import AddProviderInput

    return AddProviderInput(
        alias=alias,
        provider_type="custom",
        auth_method="api-key",
        endpoint=endpoint,
        secret="sk-test-key",
    )


def _tool_call_response(name: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": name, "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


def test_router_aliases_tool_names_for_opencode(monkeypatch, tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_tool_call_response("fs_read"))

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        provider = services.providers.add(_add_custom_input("go", OPENCODE_GO_URL))
        model = services.models.add(provider.alias, "m-x", "mx")
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        req = _request(tools=(ToolSchema(name="fs.read", description="read", parameters={}),))
        response = router.invoke(provider, model.id, req)
        body = json.loads(seen[-1].content)
        assert body["tools"][0]["function"]["name"] == "fs_read"
        assert response.tool_calls[0].name == "fs.read"
    finally:
        ctx.close()


def test_router_keeps_tool_names_off_vendor(monkeypatch, tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_tool_call_response("fs_read"))

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        provider = services.providers.add(_add_custom_input("other", "https://api.test/v1"))
        model = services.models.add(provider.alias, "m-x", "mx")
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        req = _request(tools=(ToolSchema(name="fs.read", description="read", parameters={}),))
        response = router.invoke(provider, model.id, req)
        body = json.loads(seen[-1].content)
        # F3: dotted names violate the official OpenAI/Anthropic wire
        # contract everywhere, not only on OpenCode hosts; the reversible
        # alias rewrites them and the response restores the registry name.
        assert body["tools"][0]["function"]["name"] == "fs_read"
        assert response.tool_calls[0].name == "fs.read"
    finally:
        ctx.close()


def test_router_disambiguates_colliding_aliases(monkeypatch, tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_tool_call_response("a_b__2"))

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        provider = services.providers.add(_add_custom_input("go", OPENCODE_GO_URL))
        model = services.models.add(provider.alias, "m-x", "mx")
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        req = _request(
            tools=(
                ToolSchema(name="a.b", description="dotted", parameters={}),
                ToolSchema(name="a_b", description="underscored", parameters={}),
            )
        )
        response = router.invoke(provider, model.id, req)
        body = json.loads(seen[-1].content)
        assert [t["function"]["name"] for t in body["tools"]] == ["a_b", "a_b__2"]
        assert response.tool_calls[0].name == "a_b"
    finally:
        ctx.close()


def test_router_stream_unaliases_tool_names(monkeypatch, tmp_path) -> None:
    seen: list[httpx.Request] = []
    body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"fs_read","arguments":"{}"}}]}}]}\n'
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n'
        "data: [DONE]\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=body.encode())

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        provider = services.providers.add(_add_custom_input("go", OPENCODE_GO_URL))
        model = services.models.add(provider.alias, "m-x", "mx")
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        req = _request(tools=(ToolSchema(name="fs.read", description="read", parameters={}),))
        response = router.invoke_stream(provider, model.id, req, lambda d: None)
        wire = json.loads(seen[-1].content)
        assert wire["tools"][0]["function"]["name"] == "fs_read"
        assert response.tool_calls[0].name == "fs.read"
    finally:
        ctx.close()


# -- Responses transport (OpenAI Responses API) ----------------------------------

RESPONSES_GO = "https://opencode.ai/zen/go/v1"


def _responses_ok(body_calls: list | None = None) -> dict:
    output: list = [
        {
            "type": "message",
            "id": "rs_1",
            "content": [{"type": "output_text", "text": "hola"}],
        }
    ]
    output.extend(body_calls or [])
    return {
        "id": "resp_1",
        "status": "completed",
        "model": "muse-spark-1.3-contributor",
        "output": output,
        "usage": {"input_tokens": 5, "output_tokens": 7},
    }


def test_responses_invoke_text() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_responses_ok())

    adapter = OpenAICompatibleAdapter(RESPONSES_GO, client=_client(handler))
    response = adapter.invoke(_request(session_id="ses_1"), "sk-test", None, transport="responses")
    assert str(seen[0].url).endswith("/responses")
    assert seen[0].headers["x-opencode-session"] == "ses_1"
    wire = json.loads(seen[-1].content)
    assert wire["model"] == "model-x"
    assert wire["input"][0] == {"role": "system", "content": "sys"}
    assert wire["input"][1] == {"role": "user", "content": "hola"}
    assert "tools" not in wire
    assert response.content == "hola"
    assert response.tool_calls == ()
    assert response.stop_reason is StopReason.END_TURN
    assert (response.usage.input_tokens, response.usage.output_tokens) == (5, 7)


def test_responses_invoke_tool_calls() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=_responses_ok(
                [
                    {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "c1",
                        "name": "fs_read",
                        "arguments": '{"p": "x"}',
                    }
                ]
            ),
        )

    adapter = OpenAICompatibleAdapter(RESPONSES_GO, client=_client(handler))
    req = _request(tools=(ToolSchema(name="fs.read", description="read", parameters={}),))
    response = adapter.invoke(
        req, "sk-test", None, transport="responses", tool_aliases={"fs_read": "fs.read"}
    )
    wire = json.loads(seen[-1].content)
    assert wire["tools"] == [
        {"type": "function", "name": "fs_read", "description": "read", "parameters": {}}
    ]
    (call,) = response.tool_calls
    assert (call.id, call.name, call.arguments) == ("c1", "fs_read", {"p": "x"})
    assert response.stop_reason is StopReason.TOOL_CALLS


def test_responses_history_roundtrip() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_responses_ok())

    adapter = OpenAICompatibleAdapter(RESPONSES_GO, client=_client(handler))
    req = ModelRequest(
        model="m",
        messages=(
            ChatMessage.user("do it"),
            ChatMessage.assistant(
                "", tool_calls=(ToolCall(id="c1", name="fs.read", arguments={"p": "x"}),)
            ),
            ChatMessage.tool_result("c1", "fs.read", '{"ok": true}'),
        ),
    )
    adapter.invoke(req, "sk-test", None, transport="responses", tool_aliases={"fs_read": "fs.read"})
    wire = json.loads(seen[-1].content)
    assert wire["input"] == [
        {"role": "user", "content": "do it"},
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "fs_read",
            "arguments": '{"p": "x"}',
        },
        {"type": "function_call_output", "call_id": "c1", "output": '{"ok": true}'},
    ]


def test_responses_stream() -> None:
    seen: list[httpx.Request] = []

    def sse(payload: dict) -> str:
        return "data: " + json.dumps(payload) + "\n"

    body = (
        sse({"type": "response.output_text.delta", "delta": "ho"})
        + sse({"type": "response.output_text.delta", "delta": "la"})
        + sse(
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_1",
                "delta": '{"p": ',
            }
        )
        + sse(
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_1",
                "delta": '"x"}',
            }
        )
        + sse(
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "hola"}],
                        },
                        {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "c1",
                            "name": "fs_read",
                            "arguments": '{"p": "x"}',
                        },
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 7},
                },
            }
        )
        + "data: [DONE]\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=body.encode())

    adapter = OpenAICompatibleAdapter(RESPONSES_GO, client=_client(handler))
    deltas: list[str] = []
    response = adapter.invoke_stream(
        _request(), "sk-test", None, deltas.append, transport="responses"
    )
    assert str(seen[0].url).endswith("/responses")
    wire = json.loads(seen[-1].content)
    assert wire["stream"] is True
    assert deltas == ["ho", "la"]
    assert response.content == "hola"
    (call,) = response.tool_calls
    assert (call.id, call.name, call.arguments) == ("c1", "fs_read", {"p": "x"})
    assert (response.usage.input_tokens, response.usage.output_tokens) == (5, 7)


def test_responses_stream_items_are_kept_when_the_terminal_output_is_empty() -> None:
    # The ChatGPT subscription backend (observed 2026-09-23): each item arrives
    # only in `response.output_item.done` and `response.completed` carries
    # `output: []`. Reading only the terminal output lost the answer and the
    # tool call, and the turn ended empty.
    def sse(payload: dict) -> str:
        return "data: " + json.dumps(payload) + "\n"

    reasoning = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque", "summary": []}
    call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "c1",
        "name": "fs_read",
        "arguments": '{"p": "x"}',
    }
    message = {"type": "message", "content": [{"type": "output_text", "text": "hola"}]}
    body = (
        sse({"type": "response.output_text.delta", "delta": "hola"})
        + sse({"type": "response.output_item.done", "output_index": 2, "item": call})
        + sse({"type": "response.output_item.done", "output_index": 0, "item": reasoning})
        + sse({"type": "response.output_item.done", "output_index": 1, "item": message})
        + sse(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "status": "completed",
                    "output": [],
                    "usage": {"input_tokens": 5, "output_tokens": 7},
                },
            }
        )
    )
    adapter = OpenAICompatibleAdapter(
        RESPONSES_GO,
        client=_client(lambda request: httpx.Response(200, content=body.encode())),
    )
    response = adapter.invoke_stream(
        _request(), "sk-test", None, lambda d: None, transport="responses"
    )
    assert response.content == "hola"
    (tool,) = response.tool_calls
    assert (tool.id, tool.name, tool.arguments) == ("c1", "fs_read", {"p": "x"})
    assert response.stop_reason == StopReason.TOOL_CALLS
    # The next request must replay the items, reasoning included, in order.
    assert response.continuation == {"protocol": "responses", "items": [reasoning, message, call]}
    assert (response.usage.input_tokens, response.usage.output_tokens) == (5, 7)


def test_responses_rejects_unknown_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    adapter = OpenAICompatibleAdapter("https://api.test/v1", client=_client(handler))
    with pytest.raises(InvalidUsageError):
        adapter.invoke(_request(), "sk-test", None, transport="carrier-pigeon")


def test_anthropic_rejects_responses_transport() -> None:
    adapter = AnthropicAdapter(client=_client(lambda r: httpx.Response(200, json={})))
    with pytest.raises(InvalidUsageError):
        adapter.invoke(_request(), "key", None, transport="responses")


def test_router_resolves_catalog_transport(monkeypatch, tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_responses_ok())

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        provider = services.providers.add(_add_custom_input("go", OPENCODE_GO_URL))
        model = services.models.add(provider.alias, "muse-spark-1.3-contributor", "muse13")
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        response = router.invoke(provider, model.id, _request())
        assert str(seen[-1].url).endswith("/responses")
        assert response.content == "hola"
    finally:
        ctx.close()


def test_router_explicit_transport_wins(monkeypatch, tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_chat_ok())

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        provider = services.providers.add(_add_custom_input("go", OPENCODE_GO_URL))
        model = services.models.add(
            provider.alias,
            "muse-spark-1.3-contributor",
            "muse13",
            settings={"transport": "chat"},
        )
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        router.invoke(provider, model.id, _request())
        assert str(seen[-1].url).endswith("/chat/completions")
    finally:
        ctx.close()


def test_router_rejects_unknown_transport_setting(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from rinari.models.router import _resolve_transport

    provider = SimpleNamespace(endpoint=OPENCODE_GO_URL)
    model = SimpleNamespace(
        alias="mx", provider_model_id="m-x", settings={"transport": "telepathy"}
    )
    with pytest.raises(InvalidUsageError):
        _resolve_transport(provider, model)


def test_model_add_persists_transport_settings(monkeypatch, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        provider = services.providers.add(_add_custom_input("go", OPENCODE_GO_URL))
        model = services.models.add(
            provider.alias, "m-x", "mx", settings={"transport": "responses"}
        )
        assert services.models.resolve(model.id).settings == {"transport": "responses"}
    finally:
        ctx.close()


def test_pick_saves_responses_transport_for_catalog_id(monkeypatch, tmp_path) -> None:
    from rinari.cli.commands.models import _save_and_activate

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        record = services.providers.add(_add_custom_input("go", OPENCODE_GO_URL))
        chosen = _save_and_activate(services, record, "muse-spark-1.3-contributor", None)
        assert chosen.settings.get("transport") == "responses"
        other = _save_and_activate(services, record, "deepseek-v4-flash", None)
        assert "transport" not in other.settings
    finally:
        ctx.close()


def test_long_nonconforming_name_is_truncated_into_the_wire_pattern(monkeypatch, tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        # Model calls back with the exact name it was offered: the aliases
        # are reversible, so the unaliased ToolCall restores the registry name.
        offered = payload["tools"][0]["function"]["name"]
        seen.append(request)
        return httpx.Response(200, json=_tool_call_response(offered))

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        provider = services.providers.add(_add_custom_input("other", "https://api.test/v1"))
        model = services.models.add(provider.alias, "m-x", "mx")
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        long_name = "x" * 65  # 65 chars: one over the official bound
        req = _request(tools=(ToolSchema(name=long_name, description="long", parameters={}),))
        response = router.invoke(provider, model.id, req)
        wire_name = json.loads(seen[-1].content)["tools"][0]["function"]["name"]
        # The sanitized alias must fit the 64-char pattern...
        assert len(wire_name) <= 64
        # ...and the returned call restores the original registry name.
        assert response.tool_calls[0].name == long_name
    finally:
        ctx.close()


def test_collision_suffix_stays_within_the_wire_pattern(monkeypatch, tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_tool_call_response("ok"))

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        provider = services.providers.add(_add_custom_input("other", "https://api.test/v1"))
        model = services.models.add(provider.alias, "m-x", "mx")
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        base = "y" * 65
        req = _request(
            tools=(
                ToolSchema(name=base, description="a", parameters={}),
                ToolSchema(name=base + "_dup", description="b", parameters={}),
            )
        )
        router.invoke(provider, model.id, req)
        body = json.loads(seen[-1].content)
        names = [t["function"]["name"] for t in body["tools"]]
        assert len({names[0], names[1]}) == 2, "aliases must remain distinct"
        assert all(len(n) <= 64 for n in names), names
    finally:
        ctx.close()


# -- router (original section) ---------------------------------------------------


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


def test_stream_timeout_policy_precedence_is_transport_only(monkeypatch, tmp_path):
    from dataclasses import replace

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                b"data: [DONE]\n"
            ),
        )

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        provider = services.providers.add(_add_input(services, "openai", "https://api.test/v1"))
        model = services.models.add(
            provider.alias, "gpt-real", "principal", settings={"stream_timeouts": {"idle": 77}}
        )
        (ctx.home / "model-execution.json").write_text(
            json.dumps(
                {
                    "timeouts": {"connect": 19, "first_byte": 42, "idle": 50},
                    "provider_timeouts": {provider.id: {"idle": 65}},
                }
            ),
            encoding="utf-8",
        )
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        router.invoke_stream(provider, model.id, _request(), lambda _: None)
        assert seen[-1].extensions["timeout"]["connect"] == 19
        assert seen[-1].extensions["timeout"]["read"] == 77
        router.invoke_stream(
            provider, model.id, replace(_request(), stream_timeouts={"idle": 99}), lambda _: None
        )
        assert seen[-1].extensions["timeout"]["read"] == 99
        assert b"stream_timeouts" not in seen[-1].content
    finally:
        ctx.close()


# -- F3: wire tool names conform to the official contracts everywhere ----------


def test_anthropic_tools_use_official_wire_names(monkeypatch, tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "fs_read",
                        "input": {"path": "b.txt"},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        )

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        from rinari.application.provider_service import AddProviderInput

        provider = services.providers.add(
            AddProviderInput(
                alias="anthropic-test",
                provider_type="anthropic",
                auth_method="api-key",
                secret="sk-test-key",
            )
        )
        model = services.models.add(provider.alias, "claude-x", "cx")
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        req = _request(tools=(ToolSchema(name="fs.read", description="read", parameters={}),))
        response = router.invoke(provider, model.id, req)
        body = json.loads(seen[-1].content)
        # Declaration uses the sanitized alias...
        assert body["tools"][0]["name"] == "fs_read"
        # ...and the model-requested call restores the registry name.
        assert response.tool_calls[0].name == "fs.read"
    finally:
        ctx.close()


def test_anthropic_history_uses_wire_names(monkeypatch, tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"content": [], "stop_reason": "end_turn", "usage": {}},
        )

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        from rinari.application.provider_service import AddProviderInput

        provider = services.providers.add(
            AddProviderInput(
                alias="anthropic-test",
                provider_type="anthropic",
                auth_method="api-key",
                secret="sk-test-key",
            )
        )
        model = services.models.add(provider.alias, "claude-x", "cx")
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        req = _request(
            tools=(ToolSchema(name="fs.read", description="read", parameters={}),),
            messages=(
                ChatMessage.system("sys"),
                ChatMessage.user("hola"),
                ChatMessage.assistant(
                    "",
                    tool_calls=(ToolCall(id="tu_1", name="fs.read", arguments={"p": 1}),),
                ),
                ChatMessage.tool_result("tu_1", "fs.read", "ok"),
            ),
        )
        router.invoke(provider, model.id, req)
        body = json.loads(seen[-1].content)
        tool_use_blocks = [
            block
            for message in body["messages"]
            for block in (message["content"] if isinstance(message["content"], list) else [])
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        assert tool_use_blocks[0]["name"] == "fs_read"
    finally:
        ctx.close()


def test_conforming_tool_names_pass_through_untouched(monkeypatch, tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_tool_call_response("fs_read"))

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        provider = services.providers.add(_add_custom_input("other", "https://api.test/v1"))
        model = services.models.add(provider.alias, "m-x", "mx")
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        req = _request(
            tools=(
                ToolSchema(name="fs_read", description="conforming", parameters={}),
                ToolSchema(name="web.search", description="needs alias", parameters={}),
            )
        )
        response = router.invoke(provider, model.id, req)
        body = json.loads(seen[-1].content)
        names = [t["function"]["name"] for t in body["tools"]]
        # Conforming names keep their exact spelling; only non-conforming
        # ones get the collision-safe sanitized alias (web.search -> web_search).
        assert names == ["fs_read", "web_search"]
        unaliased = {tc.name for tc in response.tool_calls}
        assert unaliased <= {"fs_read", "web.search"}
    finally:
        ctx.close()


def test_router_stream_repeats_without_a_refused_reasoning_control(monkeypatch, tmp_path) -> None:
    """OpenCode Go routes one model to several upstreams; one refused the
    control mid-turn and the whole turn failed. The call now repeats once
    without it, and the next call asks for the level again."""
    from rinari.models.types import ProviderCapabilities

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Upstream request failed: [invalid_request_error] "
                        "native reasoning control reasoning_effort is not allowed",
                    }
                },
            )
        chunk = {"choices": [{"index": 0, "delta": {"content": "hola"}, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode(),
            headers={"content-type": "text/event-stream"},
        )

    ctx, services = _app_and_services(handler, monkeypatch, tmp_path)
    try:
        provider = services.providers.add(_add_custom_input("go", OPENCODE_GO_URL))
        model = services.models.add(provider.alias, "glm-5.3-flash", "flash")
        router = ModelRouter(services.providers, services.models, http_client=_client(handler))
        monkeypatch.setattr(
            router,
            "capabilities",
            lambda *_: ProviderCapabilities(
                streaming=True, tool_calls=True, structured_output=True, reasoning_effort=True
            ),
        )
        events: list = []
        request = _request(
            reasoning_effort="medium",
            usage_observer=lambda name, payload: events.append(name),
        )
        deltas: list[str] = []
        response = router.invoke_stream(provider, model.id, request, deltas.append)
        assert response.content == "hola"
        assert deltas == ["hola"]
        assert ["reasoning_effort" in body for body in seen] == [True, False]
        assert "provider.reasoning.dropped" in events
    finally:
        ctx.close()
