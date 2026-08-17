"""HTTP capability (phase 5): generic requests, retries, SSE, auth, artifacts.

Deterministic: no sockets — httpx.MockTransport through the ctx.web factory
seam; fake sleep for backoff; fake CredentialStore for auth references.
Non-2xx statuses are data (the tool's contract is "make the request");
only transport failures raise tool error codes.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from rinari.http.auth import apply_auth, redact_headers, redact_url
from rinari.http.client import parse_retry_after, request
from rinari.http.sse import parse_sse_events
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.policy.network import NetworkGuard, NetworkPolicy
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.tools.definition import ToolContext
from rinari.tools.native.http import http_tools
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime
from rinari.web.client import WebRequestError

SSE_STREAM = """\
: a comment
event: ping
data: tick

data: one
data: two

event: done
data: {"n": 3}

"""


def _handler_by_url(routes: dict[str, httpx.Response]):
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        if key in routes:
            return routes[key]
        return httpx.Response(404, text="no route")

    return handler


def _attempt_handler(responses: list[httpx.Response]):
    """Per-attempt responses for the mock seam.

    The mock transport is probed twice per attempt (stream attempt + fallback
    full read), so both calls of a pair serve the same attempt's response.
    """
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        idx = min(state["calls"] // 2, len(responses) - 1)
        state["calls"] += 1
        return responses[idx]

    return handler


def _factory(handler) -> callable:
    def factory():
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    return factory


class _FakeCreds:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def resolve(self, ref: str) -> str:
        if ref not in self.values:
            raise RuntimeError(f"credential not found: {ref}")
        return self.values[ref]


def _ctx(tmp_path: Path, *, web=None, credentials=None, network=None) -> ToolContext:
    root = tmp_path / "work"
    root.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        session_id="s-http",
        kind="CHAT",
        cwd=root,
        project_root=None,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=30, max_output_bytes=65536),
        artifact_root=tmp_path / "artifacts",
        clock=FakeClock(),
        cancellation=CancellationToken(),
        network=network,
        web=web,
        credentials=credentials,
    )


def _tool(name: str):
    return next(t for t in http_tools() if t.name == name)


# -- retry-after parsing ---------------------------------------------------------


def test_parse_retry_after_delta_seconds() -> None:
    assert parse_retry_after("12") == 12
    assert parse_retry_after("0") == 0
    assert parse_retry_after("999999") == 30
    assert parse_retry_after(None) is None
    assert parse_retry_after("not-a-number") is None


def test_parse_retry_after_http_date() -> None:
    # RFC 1123 date; reference is 10s before it (epoch 1445412480).
    delay = parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1_445_412_470.0)
    assert delay == 10


# -- transport: request() ---------------------------------------------------------


def test_request_get_returns_data_with_redacted_headers() -> None:
    resp = httpx.Response(
        200,
        text='{"ok": true}',
        headers={
            "content-type": "application/json",
            "Set-Cookie": "sessionId=abc123",
            "X-RateLimit-Remaining": "42",
        },
    )
    out = request(
        "https://ex.com/api", client_factory=_factory(_handler_by_url({"https://ex.com/api": resp}))
    )
    assert out.status == 200
    assert out.final_url == "https://ex.com/api"
    assert out.body == b'{"ok": true}'
    assert out.attempts == 1
    assert out.headers["set-cookie"] == "***"
    assert out.headers["x-ratelimit-remaining"] == "42"
    assert not out.truncated


def test_request_post_body_json_and_query_sent() -> None:
    out = request(
        "https://ex.com/items",
        method="POST",
        body_json={"name": "widget"},
        query={"limit": "5"},
        client_factory=_factory(_attempt_handler([httpx.Response(201, text="created")])),
    )
    assert out.status == 201


def test_request_post_sends_method_headers_and_body() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["content_type"] = req.headers.get("content-type")
        captured["body"] = req.content
        captured["query"] = dict(req.url.params)
        return httpx.Response(200, text="ok")

    request(
        "https://ex.com/items",
        method="post",
        body_json={"a": 1},
        query={"x": "1"},
        headers={"X-Trace": "t1"},
        client_factory=_factory(handler),
    )
    assert captured["method"] == "POST"
    assert captured["content_type"] == "application/json"
    assert captured["body"] == b'{"a": 1}'
    assert captured["query"] == {"x": "1"}


def test_request_follows_redirects() -> None:
    routes = {
        "https://ex.com/old": httpx.Response(
            302, headers={"location": "https://ex.com/new"}, text=""
        ),
        "https://ex.com/new": httpx.Response(200, text="final"),
    }
    out = request("https://ex.com/old", client_factory=_factory(_handler_by_url(routes)))
    assert out.status == 200
    assert out.final_url == "https://ex.com/new"


def test_request_non_2xx_is_data_not_error() -> None:
    routes = {"https://ex.com/api/missing": httpx.Response(404, text='{"error": "nope"}')}
    out = request("https://ex.com/api/missing", client_factory=_factory(_handler_by_url(routes)))
    assert out.status == 404
    assert out.body == b'{"error": "nope"}'


def test_request_429_reports_retry_after() -> None:
    routes = {
        "https://ex.com/api": httpx.Response(429, text="slow down", headers={"Retry-After": "7"})
    }
    out = request("https://ex.com/api", client_factory=_factory(_handler_by_url(routes)))
    assert out.status == 429
    assert out.retry_after_s == 7
    assert out.attempts == 1
    assert out.sha256


def test_request_retries_429_then_success_uses_retry_after() -> None:
    sleeps: list[float] = []
    responses = [
        httpx.Response(429, text="slow", headers={"Retry-After": "7"}),
        httpx.Response(200, text="ok"),
    ]
    out = request(
        "https://ex.com/api",
        max_retries=1,
        client_factory=_factory(_attempt_handler(responses)),
        sleep=sleeps.append,
    )
    assert out.status == 200
    assert out.attempts == 2
    assert sleeps == [7.0]


def test_request_backoff_sequence_without_retry_after() -> None:
    sleeps: list[float] = []
    responses = [
        httpx.Response(502, text="gw"),
        httpx.Response(503, text="busy"),
        httpx.Response(200, text="ok"),
    ]
    out = request(
        "https://ex.com/api",
        max_retries=2,
        client_factory=_factory(_attempt_handler(responses)),
        sleep=sleeps.append,
    )
    assert out.status == 200
    assert out.attempts == 3
    assert sleeps == pytest.approx([0.5, 1.0])


def test_request_post_not_retried_on_5xx() -> None:
    sleeps: list[float] = []
    out = request(
        "https://ex.com/api",
        method="POST",
        content="x",
        max_retries=3,
        client_factory=_factory(_attempt_handler([httpx.Response(500, text="boom")])),
        sleep=sleeps.append,
    )
    assert out.status == 500
    assert out.attempts == 1
    assert sleeps == []


def test_request_retries_connection_errors_then_raises() -> None:
    sleeps: list[float] = []
    seen: list = []

    def boom(req: httpx.Request) -> httpx.Response:
        seen.append(1)
        raise httpx.ConnectError("refused")

    with pytest.raises(WebRequestError) as excinfo:
        request(
            "https://ex.com/api",
            max_retries=2,
            client_factory=_factory(boom),
            sleep=sleeps.append,
        )
    assert excinfo.value.code == "NETWORK_ERROR"
    assert excinfo.value.retryable
    # Transport errors fire once per attempt (no fallback read on failure).
    assert len(seen) == 3
    assert sleeps == pytest.approx([0.5, 1.0])


def test_request_timeout_maps_to_timeout_code() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(WebRequestError) as excinfo:
        request("https://ex.com/api", client_factory=_factory(boom), timeout_s=1.0)
    assert excinfo.value.code == "TIMEOUT"


def test_request_invalid_method_rejected() -> None:
    with pytest.raises(WebRequestError) as excinfo:
        request("https://ex.com/api", method="BOGUS")
    assert excinfo.value.code == "INVALID_ARGUMENT"


def test_request_body_and_body_json_conflict() -> None:
    with pytest.raises(WebRequestError) as excinfo:
        request("https://ex.com/api", method="POST", content="x", body_json={"a": 1})
    assert excinfo.value.code == "INVALID_ARGUMENT"


def test_request_cancellation_between_attempts() -> None:
    token = CancellationToken()
    token.cancel()
    with pytest.raises(WebRequestError) as excinfo:
        request("https://ex.com/api", is_cancelled=lambda: token.cancelled)
    assert excinfo.value.code == "CANCELLED"


def test_request_bounded_body_max_bytes() -> None:
    big = b"a" * 4096
    routes = {"https://ex.com/big": httpx.Response(200, content=big)}
    out = request(
        "https://ex.com/big", max_bytes=1024, client_factory=_factory(_handler_by_url(routes))
    )
    assert out.truncated
    assert len(out.body) == 1024


def test_request_cancelled_token_short_circuits_retries() -> None:
    sleeps: list[float] = []
    responses = [httpx.Response(500, text="boom")]
    with pytest.raises(WebRequestError) as excinfo:
        request(
            "https://ex.com/api",
            max_retries=2,
            client_factory=_factory(_attempt_handler(responses)),
            sleep=sleeps.append,
            is_cancelled=lambda: True,
        )
    assert excinfo.value.code == "CANCELLED"


# -- SSE ---------------------------------------------------------------------------


def test_parse_sse_events_full_semantics() -> None:
    events, stopped = parse_sse_events(SSE_STREAM.splitlines())
    assert stopped is False
    assert [e.event for e in events] == ["ping", "message", "done"]
    assert events[1].data == "one\ntwo"
    assert events[2].data == '{"n": 3}'


def test_parse_sse_events_max_events_stops_early() -> None:
    events, stopped = parse_sse_events(SSE_STREAM.splitlines(), max_events=2)
    assert stopped is True
    assert len(events) == 2


def test_parse_sse_events_id_and_retry() -> None:
    events, _ = parse_sse_events(["id: 7", "retry: 1200", "data: x", ""])
    assert events[0].last_event_id == "7"
    assert events[0].retry_ms == 1200


def test_http_sse_tool_parses_stream(tmp_path) -> None:
    routes = {
        "https://ex.com/stream": httpx.Response(
            200, text=SSE_STREAM, headers={"content-type": "text/event-stream"}
        )
    }
    ctx = _ctx(tmp_path, web=_factory(_handler_by_url(routes)))
    result = _tool("http.sse").handler({"url": "https://ex.com/stream"}, ctx)
    assert result.ok, result.error
    assert [e["event"] for e in result.data["events"]] == ["ping", "message", "done"]
    assert result.data["count"] == 3
    assert result.data["stopped_early"] is False


def test_http_sse_tool_event_filter(tmp_path) -> None:
    routes = {
        "https://ex.com/stream": httpx.Response(
            200, text=SSE_STREAM, headers={"content-type": "text/event-stream"}
        )
    }
    ctx = _ctx(tmp_path, web=_factory(_handler_by_url(routes)))
    result = _tool("http.sse").handler(
        {"url": "https://ex.com/stream", "event_filter": "done"}, ctx
    )
    assert result.ok
    assert result.data["count"] == 1
    assert result.data["events"][0]["event"] == "done"


def test_http_sse_tool_rejects_non_sse_content_type(tmp_path) -> None:
    routes = {"https://ex.com/plain": httpx.Response(200, text="hi")}
    ctx = _ctx(tmp_path, web=_factory(_handler_by_url(routes)))
    result = _tool("http.sse").handler({"url": "https://ex.com/plain"}, ctx)
    assert not result.ok
    assert result.error.code.value == "INVALID_ARGUMENT"


def test_http_sse_tool_max_events_bounded(tmp_path) -> None:
    routes = {
        "https://ex.com/stream": httpx.Response(
            200, text=SSE_STREAM, headers={"content-type": "text/event-stream"}
        )
    }
    ctx = _ctx(tmp_path, web=_factory(_handler_by_url(routes)))
    result = _tool("http.sse").handler({"url": "https://ex.com/stream", "max_events": 1}, ctx)
    assert result.ok
    assert result.data["count"] == 1
    assert result.data["stopped_early"] is True


# -- auth ---------------------------------------------------------------------------


def test_apply_auth_bearer_and_header() -> None:
    creds = _FakeCreds({"env://TOK": "sekret"})
    injection = apply_auth({"type": "bearer", "ref": "env://TOK"}, creds)
    assert injection.headers == {"Authorization": "Bearer sekret"}
    assert injection.query == {}
    assert injection.secret_query_names == ()
    injection = apply_auth({"type": "header", "name": "X-Api-Key", "ref": "env://TOK"}, creds)
    assert injection.headers == {"X-Api-Key": "sekret"}


def test_apply_auth_basic() -> None:
    import base64

    creds = _FakeCreds({"env://PW": "p4ss"})
    injection = apply_auth({"type": "basic", "username": "bot", "ref": "env://PW"}, creds)
    header = injection.headers["Authorization"]
    assert header.startswith("Basic ")
    assert base64.b64decode(header.removeprefix("Basic ")).decode() == "bot:p4ss"


def test_apply_auth_query_redacts_final_url() -> None:
    creds = _FakeCreds({"env://K": "k-123"})
    injection = apply_auth({"type": "query", "name": "api_key", "ref": "env://K"}, creds)
    assert injection.query == {"api_key": "k-123"}
    url = redact_url("https://ex.com/api?api_key=k-123&other=1", injection.secret_query_names)
    assert "k-123" not in url
    assert "other=1" in url


def test_apply_auth_requires_store() -> None:
    with pytest.raises(WebRequestError) as excinfo:
        apply_auth({"type": "bearer", "ref": "env://TOK"}, None)
    assert excinfo.value.code == "AUTH_REQUIRED"


def test_apply_auth_unresolvable_and_empty() -> None:
    with pytest.raises(WebRequestError) as excinfo:
        apply_auth({"type": "bearer", "ref": "env://MISSING"}, _FakeCreds({}))
    assert excinfo.value.code == "AUTH_REQUIRED"
    with pytest.raises(WebRequestError) as excinfo:
        apply_auth({"type": "bearer", "ref": "env://EMPTY"}, _FakeCreds({"env://EMPTY": ""}))
    assert excinfo.value.code == "AUTH_EXPIRED"


def test_apply_auth_invalid_shapes() -> None:
    for spec in (
        {"type": "carrot", "ref": "env://X"},
        {"type": "basic", "ref": "env://X"},
        {"type": "header", "name": 123, "ref": "env://X"},
        "bearer",
    ):
        with pytest.raises(WebRequestError):
            apply_auth(spec, _FakeCreds({"env://X": "v"}))


def test_redact_headers_masks_sensitive_only() -> None:
    redacted = redact_headers(
        {"Authorization": "Bearer z", "X-Api-Key": "k", "content-type": "text"}
    )
    assert redacted["Authorization"] == "***"
    assert redacted["X-Api-Key"] == "***"
    assert redacted["content-type"] == "text"


def test_redact_url_keeps_non_secret_params() -> None:
    url = redact_url("https://ex.com/a?token=t&x=1", ["token"])
    assert "token=t" not in url
    assert "x=1" in url


# -- http.request tool ---------------------------------------------------------------


def test_http_request_tool_404_is_data_with_body(tmp_path) -> None:
    routes = {"https://ex.com/api/missing": httpx.Response(404, text='{"error": "nope"}')}
    ctx = _ctx(tmp_path, web=_factory(_handler_by_url(routes)))
    result = _tool("http.request").handler({"url": "https://ex.com/api/missing"}, ctx)
    assert result.ok, result.error
    assert result.data["status"] == 404
    assert result.data["success"] is False
    assert result.data["body"] == '{"error": "nope"}'


def test_http_request_tool_rejects_bad_input(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    bad = [
        {"url": "ftp://nope"},
        {"url": "https://ex.com/x", "method": "BOGUS"},
        {"url": "https://ex.com/x", "body": "a", "body_json": {"a": 1}, "method": "POST"},
        {"url": "https://ex.com/x", "headers": "not-a-dict"},
    ]
    for args in bad:
        result = _tool("http.request").handler(args, ctx)
        assert not result.ok
        assert result.error.code.value == "INVALID_ARGUMENT", args


def test_http_request_tool_auth_injected_and_redacted(tmp_path) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["authorization"] = req.headers.get("authorization")
        return httpx.Response(
            200,
            text="ok",
            headers={"x-api-key": "echoed-from-server", "x-trace": "t"},
        )

    ctx = _ctx(tmp_path, web=_factory(handler), credentials=_FakeCreds({"env://TOK": "sekret"}))
    result = _tool("http.request").handler(
        {"url": "https://ex.com/api", "auth": {"type": "bearer", "ref": "env://TOK"}}, ctx
    )
    assert result.ok, result.error
    assert captured["authorization"] == "Bearer sekret"
    assert result.data["headers"]["x-api-key"] == "***"
    assert result.data["headers"]["x-trace"] == "t"
    assert "sekret" not in str(result.data)


def test_http_request_tool_query_auth_redacts_final_url(tmp_path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    ctx = _ctx(tmp_path, web=_factory(handler), credentials=_FakeCreds({"env://K": "k-123"}))
    result = _tool("http.request").handler(
        {
            "url": "https://ex.com/api",
            "auth": {"type": "query", "name": "api_key", "ref": "env://K"},
        },
        ctx,
    )
    assert result.ok, result.error
    assert "k-123" not in str(result.data)
    assert "api_key" in result.data["final_url"]


def test_http_request_tool_save_as_and_binary_artifact(tmp_path) -> None:
    body = b"\x89PNG fake"
    routes = {
        "https://ex.com/img.png": httpx.Response(
            200, content=body, headers={"content-type": "image/png"}
        )
    }
    ctx = _ctx(tmp_path, web=_factory(_handler_by_url(routes)))
    result = _tool("http.request").handler(
        {"url": "https://ex.com/img.png", "save_as": "logo.png"}, ctx
    )
    assert result.ok, result.error
    assert result.data["binary"] is True
    artifact = result.data["artifact"]
    path = Path(artifact["path"])
    assert path.read_bytes() == body
    assert artifact["sha256"] == _sha256(body)


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_http_request_tool_large_text_preview_plus_artifact(tmp_path) -> None:
    big = "x" * 20_000
    routes = {"https://ex.com/big": httpx.Response(200, text=big)}
    ctx = _ctx(tmp_path, web=_factory(_handler_by_url(routes)))
    result = _tool("http.request").handler({"url": "https://ex.com/big"}, ctx)
    assert result.ok, result.error
    assert "body" not in result.data
    assert result.data["body_preview"].startswith("xxx")
    assert result.data["artifact"]["bytes"] == len(big)


def test_http_request_tool_network_guard_deny(tmp_path) -> None:
    guard = NetworkGuard(NetworkPolicy(mode="off"))
    ctx = _ctx(
        tmp_path,
        network=guard,
        web=_factory(_handler_by_url({"https://ex.com/x": httpx.Response(200, text="x")})),
    )
    result = _tool("http.request").handler({"url": "https://ex.com/x"}, ctx)
    assert not result.ok
    assert result.error.code.value == "SANDBOX_VIOLATION"


def test_http_request_real_runtime_denied_by_policy(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register_all(http_tools())
    policy = PolicyEngine(network=NetworkPolicy(mode="off"))
    runtime = ToolRuntime(
        registry,
        policy,
        ApprovalEngine(prompt=lambda req: "n"),
        clock=FakeClock(),
    )
    ctx = _ctx(tmp_path, network=NetworkGuard(NetworkPolicy(mode="off")))
    result = runtime.execute("http.request", {"url": "https://ex.com/x"}, ctx)
    assert not result.ok
    assert result.error.code.value == "POLICY_DENIED"


def test_http_request_real_runtime_allowed_end_to_end(tmp_path) -> None:
    routes = {"https://ex.com/api": httpx.Response(200, text='{"ok": 1}')}
    registry = ToolRegistry()
    registry.register_all(http_tools())
    policy = PolicyEngine(network=NetworkPolicy(mode="allow"))
    runtime = ToolRuntime(
        registry,
        policy,
        ApprovalEngine(prompt=lambda req: "n"),
        clock=FakeClock(),
    )
    ctx = _ctx(
        tmp_path,
        network=NetworkGuard(NetworkPolicy(mode="allow")),
        web=_factory(_handler_by_url(routes)),
    )
    result = runtime.execute("http.request", {"url": "https://ex.com/api", "max_retries": 0}, ctx)
    assert result.ok, result.error
    assert result.data["status"] == 200
    assert result.data["body"] == '{"ok": 1}'
