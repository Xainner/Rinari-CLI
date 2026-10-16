"""Gemini's own protocol for OpenCode's @ai-sdk/google models (plan 0.4.0 #94)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from rinari.models.types import ChatMessage, ModelRequest, ToolCall, ToolSchema
from rinari.providers.adapters.google_native import GoogleNativeAdapter
from rinari.shared.errors import ProviderModelError

GO = "https://opencode.ai/zen/go/v1"


def _sse(*chunks: dict) -> bytes:
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks).encode()


def _adapter(handler) -> GoogleNativeAdapter:
    return GoogleNativeAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))


def _request(**kwargs) -> ModelRequest:
    return ModelRequest(
        model="gemini-4-argon-contributor",
        messages=(ChatMessage.system("Eres Rinari."), ChatMessage.user("hola")),
        session_id="ses_1",
        **kwargs,
    )


def test_text_goes_to_the_google_route_with_its_headers_and_reads_usage() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"candidates": [{"content": {"role": "model", "parts": [{"text": "Ho"}]}}]},
                {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "la"}]},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 3},
                },
            ),
        )

    deltas: list[str] = []
    response = _adapter(handler).invoke_stream(_request(), "sk-test", GO, deltas.append)
    assert response.content == "Hola" and "".join(deltas) == "Hola"
    assert response.usage.input_tokens == 12 and response.usage.output_tokens == 3
    request = seen[0]
    assert str(request.url) == (
        "https://opencode.ai/inference/go/google/v1beta/models/"
        "gemini-4-argon-contributor:streamGenerateContent?alt=sse"
    )
    assert request.headers["x-goog-api-key"] == "sk-test"
    assert request.headers["x-opencode-session"] == "ses_1"
    body = json.loads(request.content)
    assert body["systemInstruction"]["parts"][0]["text"] == "Eres Rinari."
    assert body["contents"][-1] == {"role": "user", "parts": [{"text": "hola"}]}


def test_a_tool_call_comes_back_and_its_signature_is_handed_back() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        part = {
            "functionCall": {"name": "fs_read", "args": {"path": "a.txt"}},
            "thoughtSignature": "sig-123",
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {
                    "candidates": [
                        {"content": {"role": "model", "parts": [part]}, "finishReason": "STOP"}
                    ]
                }
            ),
        )

    tool = ToolSchema(
        name="fs_read",
        description="Read a file",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {"path": {"type": "string", "format": "path"}},
            "required": ["path"],
        },
    )
    adapter = _adapter(handler)
    response = adapter.invoke_stream(_request(tools=(tool,)), "sk", GO, lambda d: None)
    assert response.tool_calls and response.tool_calls[0].name == "fs_read"
    assert response.tool_calls[0].arguments == {"path": "a.txt"}
    declared = bodies[0]["tools"][0]["functionDeclarations"][0]
    assert "additionalProperties" not in declared["parameters"]
    assert "format" not in declared["parameters"]["properties"]["path"]

    # The next call carries the model's parts as they were (signature included).
    follow = ModelRequest(
        model="gemini-4-argon-contributor",
        messages=(
            ChatMessage.user("lee a.txt"),
            ChatMessage.assistant(
                "",
                (ToolCall(response.tool_calls[0].id, "fs_read", {"path": "a.txt"}),),
                continuation=response.continuation,
            ),
            ChatMessage.tool_result(response.tool_calls[0].id, "fs_read", '{"text": "hola"}'),
        ),
        session_id="ses_1",
        tools=(tool,),
    )
    adapter.invoke_stream(follow, "sk", GO, lambda d: None)
    model_turn = next(c for c in bodies[1]["contents"] if c["role"] == "model")
    assert model_turn["parts"][0].get("thoughtSignature") == "sig-123"
    answer = next(
        c for c in bodies[1]["contents"] if any("functionResponse" in p for p in c["parts"])
    )
    assert answer["parts"][0]["functionResponse"]["name"] == "fs_read"


def test_gemini_errors_come_as_a_list_and_say_why() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json=[
                {
                    "error": {
                        "code": 400,
                        "message": "Invalid JSON payload: unknown field",
                        "status": "INVALID_ARGUMENT",
                    }
                }
            ],
        )

    with pytest.raises(ProviderModelError) as excinfo:
        _adapter(handler).invoke_stream(_request(), "sk", GO, lambda d: None)
    assert "Invalid JSON payload" in str(excinfo.value)


def test_opencode_google_models_choose_the_google_transport() -> None:
    from rinari.providers.metadata import model_metadata

    provider = SimpleNamespace(
        type="custom",
        endpoint=GO,
        settings={"product_id": "opencode-go"},
        auth_method="api-key",
    )
    meta = model_metadata(provider, "gemini-4-argon-contributor")
    assert meta.get("transport") == "google"
    assert meta["route_supported"] is True
