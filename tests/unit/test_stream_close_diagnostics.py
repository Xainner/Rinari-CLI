"""A stream that ends without its terminal event says how it ended.

The error used to carry only the partial text: no HTTP status, request id,
or whether the connection closed (EOF) or the provider sent [DONE] without a
finish_reason. Both cases stay failures; only the diagnosis improves.
"""

from __future__ import annotations

import json

import httpx
import pytest

from rinari.models.types import ModelRequest
from rinari.shared.errors import NetworkError
from tests.unit.test_gateway_timeout_review import (
    ADAPTERS,
    _adapter,
    _invoke_stream,
    _partial_event,
)


def _fail(kind: str, body: bytes, headers: dict | None = None) -> NetworkError:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=body, headers=headers or {})
        )
    )
    with pytest.raises(NetworkError) as err:
        _invoke_stream(
            kind, _adapter(kind, client), ModelRequest(model="test", messages=()), lambda _: None
        )
    return err.value


@pytest.mark.parametrize("kind", ADAPTERS)
def test_eof_reports_close_kind_status_and_request_id(kind):
    body = _partial_event(kind)
    error = _fail(kind, body, {"x-request-id": "req_42"})

    details = error.details
    assert error.message == "Response stream closed without a terminal event"
    assert details["kind"] == "STREAM_INTERRUPTED"
    assert details["close"] == "eof"
    assert details["http_status"] == 200
    assert details["request_id"] == "req_42"
    assert details["bytes_received"] == len(body)
    assert details["model"] == "test"
    assert "elapsed_s" in details and "idle_s" in details
    assert details["partial_text"] == "partial" and details["partial"] is True
    assert details["endpoint"].startswith("https://provider.test/")
    assert "?" not in details["endpoint"]


@pytest.mark.parametrize("kind", ["openai-chat", "openai-responses"])
def test_done_marker_without_finish_reason_is_told_apart(kind):
    error = _fail(kind, _partial_event(kind) + b"data: [DONE]\n\n")

    assert error.details["close"] == "done_marker"
    assert error.details["partial_text"] == "partial"


def test_partial_tool_call_is_flagged_and_never_returned():
    chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "id": "c1", "function": {"name": "fs.read", "arguments": "{"}}
                    ]
                }
            }
        ]
    }
    error = _fail("openai-chat", f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())

    assert error.details["partial_tool_calls"] is True
    assert error.details["partial"] is False


def test_endpoint_drops_query_and_credentials():
    from rinari.providers.adapters.http import stream_close_details

    details = stream_close_details(
        httpx.Response(200),
        {"bytes": 3, "eof": True},
        transport="chat",
        url="https://user:pw@gw.test:8443/v1/chat/completions?key=secret",
        started_at=0.0,
    )

    assert details["endpoint"] == "https://gw.test:8443/v1/chat/completions"
