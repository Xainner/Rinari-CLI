"""Provider limits and failures say what really happened (Mejoras 2026-10-04).

A quota or credits failure is not a short rate limit, the provider's own
code decides before the HTTP status, stream errors use the same taxonomy, and
every error carries what the desktop needs to explain it and link to the
provider that failed: status, provider code, retry wait and provider id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from rinari.providers.errors import (
    ProviderErrorCode,
    classify_http_error,
    classify_stream_error,
    invoke_with_retry,
)


def _resp(status: int, body=None, headers=None) -> httpx.Response:
    request = httpx.Request("POST", "https://x.test/v1/chat")
    return httpx.Response(status, request=request, json=body, headers=headers)


def test_no_credits_on_a_429_is_a_quota_not_a_rate_limit() -> None:
    """The real case: HTTP 429 «no credits left» was RATE_LIMIT and retryable."""
    err = classify_http_error(
        _resp(
            429,
            {
                "error": {
                    "message": "You exceeded your current quota.",
                    "type": "insufficient_quota",
                    "code": "insufficient_quota",
                }
            },
        ),
        "https://api.openai.com/v1/chat/completions",
        model="gpt-5.6-sol",
    )
    assert err.error_code == ProviderErrorCode.QUOTA_EXHAUSTED
    assert not err.retryable
    assert err.details["limit_kind"] == "quota"
    assert err.details["http_status"] == 429
    assert err.details["provider_response_code"] == "insufficient_quota"
    assert "current quota" in err.message
    calls = []

    def call():
        calls.append(1)
        raise err

    with pytest.raises(type(err)):
        invoke_with_retry(call, sleep=lambda _s: None)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (402, {"error": {"message": "Insufficient credits", "code": 402}}),
        (402, {"message": "Insufficient Balance"}),
        (403, {"error": {"message": "Spend cap reached", "code": "spend_limit_reached"}}),
        (400, {"type": "error", "error": {"type": "billing_not_active", "message": "x"}}),
    ],
)
def test_payment_required_and_quota_codes_win_over_the_status(status, body) -> None:
    err = classify_http_error(_resp(status, body), "https://x.test")
    assert err.error_code == ProviderErrorCode.QUOTA_EXHAUSTED
    assert err.details["http_status"] == status


def test_a_429_without_a_code_does_not_claim_to_know_which_limit() -> None:
    err = classify_http_error(_resp(429, {"error": {"message": "Slow down"}}), "https://x.test")
    assert err.error_code == ProviderErrorCode.RATE_LIMIT
    assert err.details["limit_kind"] == "unknown"
    rate = classify_http_error(
        _resp(429, {"error": {"type": "rate_limit_error", "message": "x"}}), "https://x.test"
    )
    assert rate.details["limit_kind"] == "rate"


def test_retry_after_reaches_the_details_as_seconds_or_from_a_date() -> None:
    err = classify_http_error(_resp(429, headers={"retry-after": "30"}), "https://x.test")
    assert err.retry_after == 30.0
    assert err.details["retry_after_s"] == 30.0
    when = datetime.now(UTC) + timedelta(seconds=120)
    dated = classify_http_error(
        _resp(429, headers={"retry-after": format_datetime(when, usegmt=True)}), "https://x.test"
    )
    assert 100 <= dated.details["retry_after_s"] <= 121


def test_top_level_message_and_code_are_kept() -> None:
    err = classify_http_error(
        _resp(400, {"message": "messages.3.name is not supported", "code": "bad_field"}),
        "https://opencode.ai/zen/go/v1/chat/completions",
    )
    assert "messages.3.name is not supported" in err.message
    assert err.details["provider_response_code"] == "bad_field"
    assert err.details["http_status"] == 400


def test_a_403_does_not_blame_the_key_alone() -> None:
    err = classify_http_error(
        _resp(403, {"error": {"message": "Model or operation not authorized"}}), "https://x.test"
    )
    assert err.error_code == ProviderErrorCode.AUTH
    assert "may use this model" in err.hint


def test_stream_errors_use_the_same_taxonomy_and_the_providers_words() -> None:
    timeout = classify_stream_error(
        {"type": "server_error", "message": "upstream service timeout"}, model="m"
    )
    assert timeout.error_code == ProviderErrorCode.SERVER_ERROR
    assert timeout.message == "Provider reported a stream failure: upstream service timeout"
    assert timeout.details["provider_error"] == {
        "type": "server_error",
        "message": "upstream service timeout",
    }
    quota = classify_stream_error({"code": "insufficient_quota", "message": "No credits"})
    assert quota.error_code == ProviderErrorCode.QUOTA_EXHAUSTED
    overloaded = classify_stream_error({"type": "overloaded_error", "message": "Overloaded"})
    assert overloaded.error_code == ProviderErrorCode.SERVER_ERROR
    unknown = classify_stream_error("something broke")
    assert unknown.error_code == ProviderErrorCode.STREAM_INTERRUPTED
    assert unknown.message.endswith("something broke")


def test_the_router_records_which_provider_failed(monkeypatch) -> None:
    from types import SimpleNamespace

    from rinari.models.router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    invalidated = []
    router._providers = SimpleNamespace(
        resolve_secret=lambda provider: "secret",
        _ctx=SimpleNamespace(
            config_repo=SimpleNamespace(set_json=lambda key, *_a, **_k: invalidated.append(key)),
            clock=None,
        ),
    )
    monkeypatch.setattr("rinari.shared.clock.now_iso", lambda _clock: "now")
    provider = SimpleNamespace(id="prv_1", alias="go", auth_method="api_key")
    err = classify_http_error(
        _resp(429, {"error": {"code": "insufficient_quota", "message": "x"}}), "https://x.test"
    )

    def call(_secret):
        raise err

    with pytest.raises(type(err)):
        router._authenticated_call(provider, call)
    assert err.details["provider_id"] == "prv_1"
    assert err.details["provider_alias"] == "go"
    assert invalidated == ["provider.usage.invalidated.prv_1"]
