"""Tests del cliente Anthropic nativo: /v1/messages, x-api-key, content blocks."""

import json

import httpx
import pytest

from rinari.client import LLMClient, LLMError


def make_anthropic(body: dict, status: int = 200, capture: dict | None = None):
    """Cliente Anthropic con mock transport que captura el request."""
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["url"] = str(request.url)
            capture["headers"] = dict(request.headers)
            if request.content:
                capture["body"] = json.loads(request.content)
        return httpx.Response(status, json=body)

    return LLMClient(
        base_url="https://api.anthropic.com/v1",
        api_key="sk-ant-test",
        model="claude-sonnet-4",
        provider="anthropic",
        transport=httpx.MockTransport(handler),
    )


def test_anthropic_chat_uses_messages_endpoint():
    """chat() llama POST /v1/messages (no /chat/completions)."""
    capture = {}
    client = make_anthropic(
        {"content": [{"type": "text", "text": "hola"}], "model": "claude-sonnet-4"},
        capture=capture,
    )
    out = client.chat([{"role": "user", "content": "di hola"}])
    assert out == "hola"
    assert capture["url"].endswith("/v1/messages")


def test_anthropic_headers():
    """Usa x-api-key + anthropic-version, sin Authorization Bearer."""
    capture = {}
    client = make_anthropic({"content": [{"type": "text", "text": "x"}]}, capture=capture)
    client.chat([{"role": "user", "content": "hola"}])
    h = capture["headers"]
    assert h.get("x-api-key") == "sk-ant-test"
    assert "anthropic-version" in h
    assert "authorization" not in h or "bearer" not in h.get("authorization", "").lower()


def test_anthropic_body_system_and_max_tokens():
    """System va separado en el body, y max_tokens es obligatorio."""
    capture = {}
    client = make_anthropic({"content": [{"type": "text", "text": "x"}]}, capture=capture)
    client.chat(
        [
            {"role": "system", "content": "eres tsundere"},
            {"role": "user", "content": "hola"},
        ],
        max_tokens=200,
    )
    body = capture["body"]
    assert body["system"] == "eres tsundere"
    assert body["messages"] == [{"role": "user", "content": "hola"}]
    assert body["max_tokens"] == 200
    assert body["model"] == "claude-sonnet-4"
    assert body.get("stream") is False


def test_anthropic_tool_calls_parsed():
    """chat_message() extrae tool_use blocks como tool_calls estilo OpenAI."""
    capture = {}
    client = make_anthropic(
        {
            "content": [
                {"type": "text", "text": "voy a leer"},
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "read_file",
                    "input": {"path": "a.py"},
                },
            ],
            "stop_reason": "tool_use",
        },
        capture=capture,
    )
    msg = client.chat_message([{"role": "user", "content": "lee a.py"}], tools=[{"type": "function", "function": {"name": "read_file", "description": "lee", "parameters": {"type": "object", "properties": {}}}}])
    assert msg["content"] == "voy a leer"
    tc = msg["tool_calls"]
    assert tc[0]["id"] == "toolu_01"
    assert tc[0]["function"]["name"] == "read_file"
    assert tc[0]["function"]["arguments"] == '{"path": "a.py"}'
    # el body debe llevar tools en formato Anthropic
    body = capture["body"]
    assert body["tools"][0]["name"] == "read_file"
    assert body["tools"][0]["input_schema"]["type"] == "object"


def test_anthropic_tools_sent_in_native_format():
    """Los tools OpenAI se convierten a formato Anthropic (name/input_schema)."""
    capture = {}
    client = make_anthropic({"content": [{"type": "text", "text": "ok"}]}, capture=capture)
    client.chat_message(
        [{"role": "user", "content": "usa tools"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": "ejecuta",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                },
            }
        ],
    )
    tool = capture["body"]["tools"][0]
    assert tool["name"] == "run_command"
    assert tool["description"] == "ejecuta"
    assert tool["input_schema"]["properties"]["command"]["type"] == "string"
    assert tool["input_schema"]["required"] == ["command"]


def test_anthropic_list_models_endpoint():
    """list_models usa /models con headers Anthropic."""
    capture = {}
    client = make_anthropic(
        {"data": [{"id": "claude-sonnet-4"}]}, capture=capture,
    )
    models = client.list_models()
    assert models == ["claude-sonnet-4"]
    assert capture["url"].endswith("/v1/models")


def test_anthropic_error_message():
    """Errores Anthropic (error.type/message) se muestran claros."""
    client = make_anthropic(
        {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}},
        status=401,
    )
    with pytest.raises(LLMError) as exc:
        client.chat([{"role": "user", "content": "hola"}])
    assert "bad key" in str(exc.value)


def test_openai_still_works_with_provider_openai():
    """Provider 'openai' (default) sigue usando /chat/completions."""
    capture = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capture["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = LLMClient(
        base_url="https://api.openai.com/v1",
        api_key="sk-x",
        model="gpt-4o",
        provider="openai",
        transport=httpx.MockTransport(handler),
    )
    out = client.chat([{"role": "user", "content": "hola"}])
    assert out == "ok"
    assert capture["url"].endswith("/chat/completions")
