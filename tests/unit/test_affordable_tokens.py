"""OpenRouter's 402 «can only afford N»: one retry with a lower max_tokens (plan 0.4.0 #3)."""

from __future__ import annotations

import httpx
import pytest

from rinari.models.router import within_affordable_tokens
from rinari.models.types import ChatMessage, ModelRequest, ModelResponse, StopReason
from rinari.providers.errors import (
    ProviderError,
    ProviderErrorCode,
    affordable_output_tokens,
    classify_http_error,
)

URL = "https://openrouter.ai/api/v1/chat/completions"


def _poor(affordable: int) -> ProviderError:
    error = ProviderError(
        "Provider returned HTTP 402", code=ProviderErrorCode.QUOTA_EXHAUSTED, retryable=False
    )
    error.details["affordable_output_tokens"] = affordable
    return error


def _request(max_tokens=None, observer=None):
    return ModelRequest(
        model="stepfun/step-5-preview",
        messages=(ChatMessage.user("hola"),),
        max_tokens=max_tokens,
        usage_observer=observer,
    )


def test_the_402_says_how_many_tokens_the_balance_covers() -> None:
    assert (
        affordable_output_tokens("This request requires more credits. can only afford 14814")
        == 14814
    )
    assert affordable_output_tokens("You can only afford 14,814 tokens") == 14814
    assert affordable_output_tokens("insufficient credits") is None
    response = httpx.Response(
        402,
        json={
            "error": {
                "message": "This request requires more credits, or fewer max_tokens. You "
                "requested up to 64000 tokens, but can only afford 14814.",
                "code": 402,
            }
        },
        request=httpx.Request("POST", URL),
    )
    error = classify_http_error(response, URL)
    assert error.error_code is ProviderErrorCode.QUOTA_EXHAUSTED
    assert error.details["affordable_output_tokens"] == 14814


def test_one_retry_with_ninety_percent_of_what_it_can_afford() -> None:
    seen: list = []
    events: list = []

    def call(request):
        seen.append(request.max_tokens)
        if request.max_tokens is None:
            raise _poor(14814)
        return ModelResponse(content="hola", stop_reason=StopReason.END_TURN)

    request = _request(observer=lambda name, payload: events.append((name, payload)))
    assert within_affordable_tokens(request, call).content == "hola"
    assert seen == [None, 13332]
    assert events == [
        (
            "provider.max_tokens.reduced",
            {"model": "stepfun/step-5-preview", "max_tokens": 13332, "affordable": 14814},
        )
    ]


@pytest.mark.parametrize(
    ("affordable", "max_tokens", "started"),
    [
        (500, None, False),  # too little to be worth it
        (20000, 8000, False),  # already under what it can afford
        (14814, None, True),  # output already reached the user
    ],
)
def test_no_retry_when_it_would_not_help(affordable, max_tokens, started) -> None:
    calls: list = []

    def call(request):
        calls.append(request.max_tokens)
        raise _poor(affordable)

    with pytest.raises(ProviderError):
        within_affordable_tokens(_request(max_tokens), call, output_started=lambda: started)
    assert len(calls) == 1


def test_a_second_402_is_the_balance_error_as_before() -> None:
    calls: list = []

    def call(request):
        calls.append(request.max_tokens)
        raise _poor(14814)

    with pytest.raises(ProviderError) as excinfo:
        within_affordable_tokens(_request(), call)
    assert excinfo.value.error_code is ProviderErrorCode.QUOTA_EXHAUSTED
    assert calls == [None, 13332]
