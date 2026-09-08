"""Etapa D — provider runtime: items, error taxonomy, model retry, cancellation."""

from __future__ import annotations

import httpx
import pytest

from rinari.models.types import ModelResponse
from rinari.providers.adapters.anthropic import _response_from_anthropic
from rinari.providers.adapters.openai_compatible import (
    _response_from_openai,
    _response_from_responses,
)
from rinari.providers.errors import (
    ProviderError,
    ProviderErrorCode,
    classify_http_error,
    invoke_with_retry,
)
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.errors import ProviderModelError


def _resp(status: int, **kwargs) -> httpx.Response:
    request = httpx.Request("POST", "https://x.test/v1/chat")
    return httpx.Response(status, request=request, **kwargs)


# -- ModelItem -----------------------------------------------------------------


def test_response_items_default_empty() -> None:
    assert ModelResponse(content="hi").items == ()
    assert ModelResponse(content="hi").provider_state is None


def test_openai_chat_items_mirror_content_and_tools() -> None:
    data = {
        "id": "chatcmpl-1",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "hello",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "fs.read", "arguments": '{"path": "a"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    }
    response = _response_from_openai(data, "https://x.test")
    assert response.content == "hello"
    assert [c.name for c in response.tool_calls] == ["fs.read"]
    kinds = [(i.type, i.id) for i in response.items]
    assert ("text", None) in kinds
    assert ("function_call", "call_1") in kinds


def test_anthropic_preserves_opaque_blocks_as_items() -> None:
    data = {
        "content": [
            {"type": "thinking", "thinking": "opaque-reasoning", "signature": "sig"},
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "tu_1", "name": "fs.read", "input": {"path": "a"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }
    response = _response_from_anthropic(data, "https://x.test")
    assert response.content == "hi"
    assert [c.name for c in response.tool_calls] == ["fs.read"]
    thinking = [i for i in response.items if i.type == "thinking"]
    assert len(thinking) == 1
    assert thinking[0].data["signature"] == "sig"


def test_responses_items_cover_output_kinds() -> None:
    data = {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"type": "message", "id": "m_1", "content": [{"type": "output_text", "text": "ok"}]},
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "fs.read",
                "arguments": '{"path": "a"}',
            },
        ],
        "usage": {"input_tokens": 4, "output_tokens": 6},
    }
    response = _response_from_responses(data, "https://x.test")
    kinds = {i.type for i in response.items}
    assert {"reasoning", "message", "function_call"} <= kinds


# -- error taxonomy --------------------------------------------------------------


def test_classify_auth() -> None:
    err = classify_http_error(_resp(401), "https://x.test", provider="p", model="m")
    assert isinstance(err, ProviderModelError)
    assert err.error_code == ProviderErrorCode.AUTH
    assert not err.retryable
    assert err.provider == "p" and err.model == "m"


def test_classify_rate_limit_with_retry_after() -> None:
    err = classify_http_error(_resp(429, headers={"retry-after": "7"}), "https://x.test", model="m")
    assert err.error_code == ProviderErrorCode.RATE_LIMIT
    assert err.retryable
    assert err.retry_after == 7.0


def test_classify_server_error_retryable() -> None:
    err = classify_http_error(_resp(500), "https://x.test")
    assert err.error_code == ProviderErrorCode.SERVER_ERROR
    assert err.retryable


def test_classify_model_not_found_and_timeout() -> None:
    assert classify_http_error(_resp(404), "https://x.test").error_code == (
        ProviderErrorCode.MODEL_NOT_FOUND
    )
    timeout = classify_http_error(_resp(408), "https://x.test")
    assert timeout.error_code == ProviderErrorCode.TIMEOUT
    assert timeout.retryable


# -- model retry -------------------------------------------------------------------


def test_retryable_model_calls_retry_then_succeed() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise classify_http_error(_resp(500), "https://x.test")
        return "ok"

    assert invoke_with_retry(flaky, sleep=sleeps.append) == "ok"
    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]


def test_non_retryable_model_error_no_retry() -> None:
    calls = {"n": 0}

    def denied():
        calls["n"] += 1
        raise classify_http_error(_resp(401), "https://x.test")

    with pytest.raises(ProviderError):
        invoke_with_retry(denied, sleep=lambda s: None)
    assert calls["n"] == 1


# -- cancellation ----------------------------------------------------------------------


def test_cancel_fires_callbacks_and_suppresses_their_errors() -> None:
    token = CancellationToken()
    seen: list[str] = []

    def boom():
        raise RuntimeError("listener blew up")

    token.on_cancel(lambda: seen.append("a"))
    token.on_cancel(boom)
    token.on_cancel(lambda: seen.append("b"))
    token.cancel()
    assert seen == ["a", "b"]
    assert token.cancelled
