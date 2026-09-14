"""Independent Gateway review of model streaming timeout behavior."""

from __future__ import annotations

import contextlib
import json
import math
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from rinari.engine_protocol.errors import from_rinari_error
from rinari.models.router import _stream_read_timeout_s
from rinari.models.types import ModelRequest, ProviderCapabilities
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PolicyEngine
from rinari.prompts.assembler import AssemblerContext, PromptAssembler
from rinari.providers.adapters.anthropic import AnthropicAdapter
from rinari.providers.adapters.openai_compatible import OpenAICompatibleAdapter
from rinari.providers.adapters.responses import OpenAIResponsesAdapter
from rinari.runtime.agent import AgentContext, AgentLoop
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.errors import InvalidUsageError, NetworkError
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime

ADAPTERS = ("openai-chat", "openai-responses", "anthropic")


def _adapter(kind: str, client: httpx.Client):
    if kind == "openai-chat":
        return OpenAICompatibleAdapter("https://provider.test/v1", client=client)
    if kind == "openai-responses":
        return OpenAIResponsesAdapter("https://provider.test/v1", client=client)
    if kind == "anthropic":
        return AnthropicAdapter(client=client)
    raise AssertionError(f"unknown adapter test case: {kind}")


def _invoke_stream(kind: str, adapter, request: ModelRequest, on_delta):
    endpoint = "https://provider.test" if kind == "anthropic" else None
    return adapter.invoke_stream(request, "synthetic-secret", endpoint, on_delta)


def _partial_event(kind: str) -> bytes:
    if kind == "openai-chat":
        payload = {"choices": [{"delta": {"content": "partial"}}]}
    elif kind == "openai-responses":
        payload = {"type": "response.output_text.delta", "delta": "partial"}
    else:
        payload = {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "partial"},
        }
    return f"data: {json.dumps(payload)}\n\n".encode()


class _PartialThenTimeout(httpx.SyncByteStream):
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __iter__(self):
        yield self.payload
        raise httpx.ReadTimeout("synthetic idle timeout")

    def close(self) -> None:
        return None


@pytest.mark.parametrize("kind", ADAPTERS)
def test_read_timeout_before_headers_is_structured_and_timeout_is_transport_only(kind: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ReadTimeout("synthetic header timeout", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    request = ModelRequest(model="review-model", messages=(), stream_read_timeout_s=17.0)
    deltas: list[str] = []

    with pytest.raises(NetworkError) as excinfo:
        _invoke_stream(kind, _adapter(kind, client), request, deltas.append)

    assert len(seen) == 1
    assert seen[0].extensions["timeout"]["read"] == 17.0
    assert "stream_read_timeout_s" not in seen[0].content.decode()
    assert deltas == []
    expected = {
        "kind": "TIMEOUT",
        "phase": "response_headers",
        "timeout_s": 17.0,
        "last_payload_at_s": None,
        "payload_idle_s": excinfo.value.details["payload_idle_s"],
        "headers_received": False,
        "saw_payload": False,
        "partial": False,
        "model": "review-model",
    }
    assert expected.items() <= excinfo.value.details.items()
    assert excinfo.value.details["payload_idle_s"] >= 0


@pytest.mark.parametrize("kind", ADAPTERS)
def test_partial_sse_timeout_is_terminal_and_never_replayed(kind: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            request=request,
            stream=_PartialThenTimeout(_partial_event(kind)),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    request = ModelRequest(model="review-model", messages=(), stream_read_timeout_s=23.0)
    deltas: list[str] = []

    with pytest.raises(NetworkError) as excinfo:
        _invoke_stream(kind, _adapter(kind, client), request, deltas.append)

    assert calls == 1
    assert deltas == ["partial"]
    details = excinfo.value.details
    assert details["phase"] == "between_chunks"
    assert details["timeout_s"] == 23.0
    assert details["headers_received"] is True
    assert details["saw_payload"] is True
    assert details["partial"] is True
    assert details["last_payload_at_s"] is not None
    assert details["payload_idle_s"] >= 0


@pytest.mark.parametrize(
    ("environment", "provider", "model", "expected"),
    [
        (None, {}, {}, 30.0),
        ("41", {}, {}, 41.0),
        ("41", {"stream_read_timeout_s": 52}, {}, 52.0),
        (
            "41",
            {"stream_read_timeout_s": 52},
            {"stream_read_timeout_s": 63},
            63.0,
        ),
    ],
)
def test_stream_timeout_configuration_precedence(
    monkeypatch,
    environment: str | None,
    provider: dict,
    model: dict,
    expected: float,
) -> None:
    monkeypatch.delenv("RINARI_MODEL_STREAM_READ_TIMEOUT_SECONDS", raising=False)
    if environment is not None:
        monkeypatch.setenv("RINARI_MODEL_STREAM_READ_TIMEOUT_SECONDS", environment)
    assert _stream_read_timeout_s(provider, model) == expected


@pytest.mark.parametrize("source", ("provider", "model"))
@pytest.mark.parametrize(
    "invalid",
    (True, False, math.nan, math.inf, -math.inf, 0, 0.5, 600.1, 601, "not-a-number"),
)
def test_stream_timeout_rejects_invalid_saved_values(source: str, invalid: object) -> None:
    provider = {"stream_read_timeout_s": 60.0}
    model: dict[str, object] = {}
    if source == "provider":
        provider["stream_read_timeout_s"] = invalid
    else:
        model["stream_read_timeout_s"] = invalid
    with pytest.raises(InvalidUsageError):
        _stream_read_timeout_s(provider, model)


@pytest.mark.parametrize("invalid", ("nan", "inf", "-inf", "0", "0.5", "600.1", "bad"))
def test_stream_timeout_rejects_invalid_environment_values(monkeypatch, invalid: str) -> None:
    monkeypatch.setenv("RINARI_MODEL_STREAM_READ_TIMEOUT_SECONDS", invalid)
    with pytest.raises(InvalidUsageError):
        _stream_read_timeout_s({}, {})


class _DelayedHeadersHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        time.sleep(1.2)
        body = (
            b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return None


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        return None


def test_real_loopback_delayed_headers_obey_per_request_bound() -> None:
    server = _QuietThreadingHTTPServer(("127.0.0.1", 0), _DelayedHeadersHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1"
    try:
        with httpx.Client() as short_client:
            short = OpenAICompatibleAdapter(endpoint, client=short_client)
            with pytest.raises(NetworkError) as excinfo:
                short.invoke_stream(
                    ModelRequest(model="local", messages=(), stream_read_timeout_s=1.0),
                    None,
                    None,
                    lambda _delta: None,
                )
        assert excinfo.value.details["phase"] == "response_headers"
        assert excinfo.value.details["timeout_s"] == 1.0

        deltas: list[str] = []
        with httpx.Client() as long_client:
            long = OpenAICompatibleAdapter(endpoint, client=long_client)
            response = long.invoke_stream(
                ModelRequest(model="local", messages=(), stream_read_timeout_s=2.0),
                None,
                None,
                deltas.append,
            )
        assert response.content == "ok"
        assert deltas == ["ok"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@dataclass
class _TimeoutModel:
    error: NetworkError

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False)

    def invoke(self, request: ModelRequest):
        raise self.error


def test_timeout_details_survive_engine_mapping_and_runtime_activity() -> None:
    details = {
        "kind": "TIMEOUT",
        "phase": "between_chunks",
        "timeout_s": 30.0,
        "last_payload_at_s": 4.25,
        "partial": True,
    }
    error = NetworkError("Timed out streaming from provider", details=details)

    protocol_error = from_rinari_error(error)
    assert protocol_error.code == "NETWORK_FAILURE"
    assert protocol_error.retryable is True
    assert protocol_error.details == details

    activity: list[tuple[str, dict]] = []
    runtime = ToolRuntime(ToolRegistry(), PolicyEngine(), ApprovalEngine())
    context = AgentContext(
        session_id="review-session",
        model_ref="review-model",
        tool_ctx=SimpleNamespace(cancellation=CancellationToken()),
        assembler_base=AssemblerContext(),
    )
    loop = AgentLoop(
        _TimeoutModel(error),
        runtime,
        PromptAssembler(),
        activity_sink=lambda event, payload: activity.append((event, payload)),
    )

    with pytest.raises(NetworkError):
        loop.turn(context, "trigger timeout")

    failed = next(payload for event, payload in activity if event == "model.failed")
    assert failed["error"]["code"] == "NETWORK_FAILURE"
    assert failed["error"]["retryable"] is True
    assert failed["error"]["details"] == details


@pytest.mark.parametrize("kind", ADAPTERS)
def test_eof_after_partial_content_is_failure(kind):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=_partial_event(kind)))
    )
    with pytest.raises(NetworkError) as err:
        _invoke_stream(
            kind, _adapter(kind, client), ModelRequest(model="test", messages=()), lambda _: None
        )
    assert err.value.details["kind"] == "STREAM_INTERRUPTED"
    assert err.value.details["partial_text"] == "partial"
