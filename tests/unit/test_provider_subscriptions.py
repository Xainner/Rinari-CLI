import json
from dataclasses import replace

import httpx
import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.models.router import ModelRouter
from rinari.models.types import ChatMessage, ModelRequest
from rinari.providers.adapters.anthropic import AnthropicAdapter
from rinari.providers.adapters.subscriptions import CodexResponsesAdapter
from rinari.providers.auth import ProviderAuthService
from rinari.providers.catalog import PROVIDER_CATALOG, product_for
from rinari.providers.metadata import model_metadata
from rinari.providers.usage import ProviderUsageService, amount, percent
from rinari.shared.errors import InvalidUsageError


def setup(app_ctx, handler, *, product="opencode-go", alias="account", auth="api-key"):
    services = build_services(
        app_ctx, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    preset = next(p for p in PROVIDER_CATALOG if p.key == product)
    provider = services.providers.add(
        AddProviderInput(
            alias=alias,
            provider_type=preset.provider_type,
            endpoint=preset.base_url,
            auth_method=auth,
            secret="synthetic-test-key" if auth == "api-key" else None,
            settings={"product_id": product},
        )
    )
    return services, provider


def test_go_quota_cache_rotation_and_stale(app_ctx):
    calls = []
    unavailable = [False]

    def handler(req):
        calls.append(req)
        if unavailable[0]:
            return httpx.Response(429, headers={"Retry-After": "600"})
        return httpx.Response(
            200,
            json={
                "usage": {
                    k: {"percent": p, "resetsAt": "2026-09-28T00:00:00Z"}
                    for k, p in [("rolling", 0), ("weekly", 100), ("monthly", 63)]
                }
            },
        )

    services, p = setup(app_ctx, handler)
    now = [1_790_000_000]
    usage = ProviderUsageService(services.providers, clock=lambda: now[0])
    snapshot = usage.get(p.id)
    assert [w["remaining_percent"] for w in snapshot["windows"]] == [100, 0, 37]
    assert snapshot["status"] == "available"
    assert usage.get(p.id) == snapshot
    assert len(calls) == 1
    now[0] += 310
    unavailable[0] = True
    stale = usage.get(p.id)
    assert stale["status"] == "stale"
    assert stale["fetched_at"] == snapshot["fetched_at"]
    usage.get(p.id, refresh=True)
    assert len(calls) == 2  # manual refresh respects Retry-After
    services.providers.set_auth(p.id, secret="rotated-synthetic-key")
    changed = usage.get(p.id)
    assert changed["windows"] == []  # previous account data never reused
    assert len(calls) == 3


def test_openrouter_key_unlimited_is_not_account_balance(app_ctx):
    def handler(req):
        if req.url.path.endswith("credits"):
            return httpx.Response(200, json={"data": {"total_credits": 10, "total_usage": 11.25}})
        return httpx.Response(200, json={"data": {"limit": None, "limit_remaining": None}})

    s, p = setup(app_ctx, handler, product="openrouter")
    balances = ProviderUsageService(s.providers).get(p.id)["balances"]
    assert balances[0]["remaining"] == "-1.25"
    assert balances[0]["unlimited"] is False
    assert balances[1]["scope"] == "key"
    assert balances[1]["unlimited"] is True
    assert balances[1]["remaining"] is None


def test_deepseek_keeps_decimal_currencies_separate(app_ctx):
    payload = {
        "balance_infos": [
            {
                "currency": c,
                "total_balance": "0.000000001",
                "granted_balance": "0",
                "topped_up_balance": "0.000000001",
            }
            for c in ("USD", "CNY")
        ]
    }
    s, p = setup(app_ctx, lambda req: httpx.Response(200, json=payload), product="deepseek")
    data = ProviderUsageService(s.providers).get(p.id)
    assert [b["currency"] for b in data["balances"]] == ["USD", "CNY"]
    assert data["balances"][0]["remaining"] == "0.000000001"


@pytest.mark.parametrize("bad", [None, True, -1, 101, float("nan"), "10"])
def test_invalid_percentage_is_unknown(bad):
    assert percent(bad) is None


def test_nonfinite_money_is_unknown():
    assert amount("NaN") is None
    assert amount("Infinity") is None


def test_custom_endpoint_never_inherits_product_or_sends_quota_request(app_ctx):
    s, p = setup(app_ctx, lambda req: pytest.fail("No remote quota request expected"))
    p.endpoint = "https://api.xainner.com/v1"
    s.ctx.provider_repo.update(p)
    assert product_for(p) == "custom"
    assert model_metadata(p, "glm-5.3-flash").get("max_context_tokens") is None
    assert ProviderUsageService(s.providers).get(p.id)["status"] == "unsupported"


def test_glm_catalog_scoped_to_go_and_override_survives(app_ctx):
    s, p = setup(app_ctx, lambda req: httpx.Response(200, json={"data": [{"id": "glm-5.3-flash"}]}))
    metadata = model_metadata(p, "glm-5.3-flash")
    assert metadata["max_context_tokens"] == 1_000_000
    assert metadata["vision"] is True
    m = s.models.add(
        p.id, "glm-5.3-flash", "glm", capabilities={"max_context_tokens": 64000, "vision": False}
    )
    s.models.refresh(p.id)
    router = ModelRouter(s.providers, s.models, s.providers._client)
    assert router.capabilities(p, m.id).max_context_tokens == 64000
    assert router.capabilities(p, m.id).vision is False
    assert s.models.resolve(m.id).capabilities == m.capabilities
    assert "max_context_tokens" not in s.models.resolve(m.id).settings["discovered_capabilities"]


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://api.anthropic.com", "/v1/messages"),
        ("https://api.anthropic.com/v1/", "/v1/messages"),
        ("https://gateway.test/anthropic/v1", "/anthropic/v1/messages"),
        ("https://gateway.test/prefix", "/prefix/v1/messages"),
    ],
)
def test_anthropic_paths(base, expected):
    assert httpx.URL(AnthropicAdapter()._messages_url(base)).path == expected


@pytest.mark.parametrize(
    "model,route",
    [
        ("gpt-5.6-luna", "/responses"),
        ("glm-5.3-flash", "/chat/completions"),
        ("minimax-m2.7", "/messages"),
    ],
)
@pytest.mark.parametrize("product", ["opencode-go", "opencode-zen"])
def test_go_routes_and_session_affinity(app_ctx, model, route, product):
    seen = []

    def handler(req):
        seen.append(req)
        if route == "/responses":
            return httpx.Response(200, json={"status": "completed", "output": []})
        if route == "/messages":
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    s, p = setup(app_ctx, handler, product=product)
    m = s.models.add(p.id, model, "test")
    ModelRouter(s.providers, s.models, s.providers._client).invoke(
        p,
        m.id,
        ModelRequest(model=model, messages=(ChatMessage.user("hello"),), session_id="rinari-test"),
    )
    assert str(seen[0].url).endswith(route)
    assert seen[0].headers["x-opencode-session"] == "rinari-test"


def test_claude_reasoning_and_signed_continuation_scoped_and_persisted(app_ctx):
    signed = [
        {"type": "thinking", "thinking": "private", "signature": "signed-test"},
        {"type": "tool_use", "id": "call-1", "name": "read_file", "input": {}},
    ]
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"content": signed, "stop_reason": "tool_use"})

    s, p = setup(app_ctx, handler, product="anthropic")
    m = s.models.add(p.id, "claude-sonnet-4-6", "claude")
    router = ModelRouter(s.providers, s.models, s.providers._client)
    request = ModelRequest(
        model=m.provider_model_id, messages=(ChatMessage.user("read"),), reasoning_effort="high"
    )
    response = router.invoke(p, m.id, request)
    assert calls[0]["thinking"] == {"type": "adaptive"}
    assert calls[0]["output_config"] == {"effort": "high"}
    message = ChatMessage.assistant(
        response.content, response.tool_calls, continuation=response.continuation
    )
    from rinari.cli.agent_runtime import _message_to_record, _record_to_message

    record = _message_to_record(s, "session-test", message, "2026-09-22T00:00:00Z")
    restored = _record_to_message(record)
    follow = replace(
        request,
        messages=(
            *request.messages,
            restored,
            ChatMessage.tool_result("call-1", "read_file", "result"),
        ),
    )
    router.invoke(p, m.id, follow)
    assert calls[1]["messages"][1]["content"] == signed
    other = replace(p, id="other-account")
    prepared = router.generation_request(other, m, follow)
    assert all(msg.continuation is None for msg in prepared.messages)
    with pytest.raises(InvalidUsageError):
        router.invoke(p, m.id, replace(request, reasoning_effort="xhigh"))


def test_claude_stream_preserves_thinking_and_signature():
    events = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "private"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "signature"},
        },
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "answer"},
        },
        {"type": "message_stop"},
    ]
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, text="\n\n".join("data: " + json.dumps(e) for e in events)
            )
        )
    )
    visible = []
    result = AnthropicAdapter(client=client).invoke_stream(
        ModelRequest(model="claude-sonnet-4-6", messages=()), "key", None, visible.append
    )
    assert visible == ["answer"]
    assert result.continuation["blocks"][0] == {
        "type": "thinking",
        "thinking": "private",
        "signature": "signature",
    }


def test_device_login_cancel_and_completed_credentials_never_exposed(app_ctx):
    def handler(req):
        if req.url.path.endswith("/device/code"):
            return httpx.Response(
                200,
                json={
                    "device_code": "private-device-test",
                    "user_code": "PUBLIC-CODE",
                    "interval": 5,
                },
            )
        return httpx.Response(200, json={"access_token": "private-token-test"})

    s, p = setup(app_ctx, handler, product="github-copilot", auth="oauth")
    auth = ProviderAuthService(s.providers)
    op = auth.start(p.id, "device")
    assert op["user_code"] == "PUBLIC-CODE"
    assert "private-device-test" not in json.dumps(op)
    result = auth.get(p.id, op["operation_id"])
    assert result["status"] == "connected"
    assert "private-token-test" not in json.dumps(result)
    assert s.providers.get(p.id).auth_method == "oauth"
    assert s.providers.resolve_secret(s.providers.get(p.id)) == "private-token-test"
    next_op = auth.start(p.id, "device")
    auth.cancel(p.id)
    assert auth.get(p.id, next_op["operation_id"])["status"] == "cancelled"
    assert auth.logout(p.id)["status"] == "disconnected"
    assert s.providers.credential_ref(p) is None


def test_chatgpt_refresh_reuses_rotated_credential(app_ctx):
    requests = []

    def handler(req):
        requests.append(req)
        return httpx.Response(
            200,
            json={
                "access_token": "new-test-token",
                "refresh_token": "rotated-test-refresh",
                "expires_in": 3600,
            },
        )

    s, p = setup(app_ctx, handler, product="chatgpt", auth="oauth")
    s.providers.set_oauth(
        p.id,
        {"access_token": "old-test-token", "refresh_token": "old-test-refresh", "expires_at": 0},
    )
    assert s.providers.resolve_secret(p) == "new-test-token"
    assert s.providers.resolve_secret(p) == "new-test-token"
    assert len(requests) == 1
    assert b"grant_type=refresh_token" in requests[0].content


def test_codex_payload_keeps_rinari_instructions_and_disables_storage():
    request = ModelRequest(
        model="test",
        messages=(ChatMessage.system("Rinari instructions"), ChatMessage.user("hello")),
        max_tokens=1000,
        reasoning_effort="high",
    )
    payload = CodexResponsesAdapter()._responses_payload(request, stream=False)
    assert payload["instructions"] == "Rinari instructions"
    assert payload["stream"] is True
    assert payload["store"] is False
    assert "max_output_tokens" not in payload
    assert all(m.get("role") != "system" for m in payload["input"])


def test_refresh_concurrent_and_auth_retry_before_output_only(app_ctx):
    from concurrent.futures import ThreadPoolExecutor

    from rinari.providers.errors import ProviderError, ProviderErrorCode

    calls = []
    s, p = setup(
        app_ctx,
        lambda req: (
            calls.append(req)
            or httpx.Response(
                200,
                json={
                    "access_token": "new-token",
                    "refresh_token": "next-refresh",
                    "expires_in": 3600,
                },
            )
        ),
        product="chatgpt",
        auth="oauth",
    )
    s.providers.set_oauth(
        p.id, {"access_token": "old-token", "refresh_token": "refresh", "expires_at": 0}
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert (
            list(pool.map(lambda _: s.providers.resolve_secret(p), range(4))) == ["new-token"] * 4
        )
    assert len(calls) == 1
    router = ModelRouter(s.providers, s.models, s.providers._client)
    s.providers.set_oauth(
        p.id,
        {"access_token": "rejected-token", "refresh_token": "refresh", "expires_at": 9999999999},
    )
    attempts = []

    def call(secret):
        attempts.append(secret)
        if secret == "rejected-token":
            raise ProviderError("rejected", code=ProviderErrorCode.AUTH)
        return "ok"

    assert router._authenticated_call(p, call) == "ok"
    assert attempts == ["rejected-token", "new-token"]
    s.providers.set_oauth(
        p.id,
        {"access_token": "rejected-token", "refresh_token": "refresh", "expires_at": 9999999999},
    )
    with pytest.raises(ProviderError):
        router._authenticated_call(p, call, output_started=lambda: True)
    assert len(calls) == 2


def test_browser_pkce_invalid_state_then_success_and_port_busy(app_ctx, monkeypatch):
    from http.server import HTTPServer
    from urllib.parse import parse_qs, urlsplit

    import rinari.providers.auth as module

    seen = []
    s, p = setup(
        app_ctx,
        lambda req: (
            seen.append(req)
            or httpx.Response(
                200,
                json={
                    "access_token": "browser-token",
                    "refresh_token": "browser-refresh",
                },
            )
        ),
        product="chatgpt",
        auth="oauth",
    )
    servers = []

    def ephemeral(address, handler):
        server = HTTPServer(("127.0.0.1", 0), handler)
        servers.append(server)
        return server

    monkeypatch.setattr(module, "HTTPServer", ephemeral)
    auth = ProviderAuthService(s.providers)
    op = auth.start(p.id)
    args = parse_qs(urlsplit(op["authorization_url"]).query)
    assert args["code_challenge_method"] == ["S256"]
    callback = f"http://127.0.0.1:{servers[0].server_port}/auth/callback"
    try:
        assert httpx.get(callback, params={"state": "invalid", "code": "test"}).status_code == 400
        assert seen == []
        assert (
            httpx.get(callback, params={"state": args["state"][0], "code": "test"}).status_code
            == 200
        )
        assert auth.get(p.id, op["operation_id"])["status"] == "connected"
        assert b"code_verifier=" in seen[0].content

        def occupied(*args):
            raise OSError("occupied")

        monkeypatch.setattr(module, "HTTPServer", occupied)
        assert auth.start(p.id)["status"] == "error"
        assert s.providers.resolve_secret(p) == "browser-token"
    finally:
        auth.close()


def test_device_expiration_slowdown_and_account_isolation(app_ctx):
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.path.endswith("/device/code"):
            return httpx.Response(
                200, json={"device_code": "secret", "user_code": "CODE", "interval": 5}
            )
        return httpx.Response(200, json={"error": "slow_down"})

    s, p = setup(app_ctx, handler, product="github-copilot", auth="oauth")
    auth = ProviderAuthService(s.providers)
    op = auth.start(p.id, "device")
    assert auth.get(p.id, op["operation_id"])["status"] == "waiting"
    assert auth.operations[op["operation_id"]]["interval"] == 10
    auth.get(p.id, op["operation_id"])
    assert len(calls) == 2
    auth.operations[op["operation_id"]]["expires_at"] = 0
    assert auth.get(p.id, op["operation_id"])["status"] == "expired"
    assert s.providers.credential_ref(p) is None


def test_continuation_database_roundtrip_and_local_usage_scope(app_ctx, tmp_path):
    from rinari.cli.agent_runtime import _message_to_record, _record_to_message
    from rinari.storage.records import SessionEventRecord

    s, p = setup(
        app_ctx, lambda req: httpx.Response(200, json={"usage": {"rolling": {"percent": 0}}})
    )
    s.models.add(p.id, "glm-5.3-flash", "glm")
    s.providers.use(p.id)
    session = s.sessions.new(tmp_path, forced_chat=True)
    opaque = {"protocol": "anthropic", "blocks": [{"type": "thinking", "signature": "private-sig"}]}
    message = ChatMessage.assistant("visible", continuation=opaque)
    record = _message_to_record(s, session.id, message, "2026-09-22T00:00:00Z")
    s.ctx.message_repo.append_many(session.id, [record])
    stored = s.ctx.message_repo.list(session.id)[0]
    assert _record_to_message(stored).continuation == opaque
    fork = s.sessions.fork(session.id).session
    assert s.ctx.message_repo.list(fork.id)[0].continuation == opaque
    from rinari.cli.session_export import export_session

    assert "private-sig" not in json.dumps(export_session(s, session.id))
    assert "private-sig" not in repr(stored)
    for i, provider_id in enumerate((p.id, "other", None)):
        s.ctx.event_repo.insert(
            SessionEventRecord(
                id=f"usage-{i}",
                session_id=session.id,
                seq=0,
                type="ModelInvoked",
                payload={
                    "provider_id": provider_id,
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                },
            )
        )
    usage = ProviderUsageService(s.providers).get(p.id)
    assert usage["local_usage"] == {"calls": 1, "input_tokens": 10, "output_tokens": 4}


def test_chatgpt_multiple_quota_groups_and_missing_monthly(app_ctx):
    data = {
        "rate_limit": {"primary_window": {"used_percent": 0, "limit_window_seconds": 18000}},
        "additional_rate_limits": [
            {"limit_name": "review", "rate_limit": {"secondary_window": {"used_percent": 100}}}
        ],
        "credits": {"balance": "-0.01", "unlimited": False},
    }
    s, p = setup(
        app_ctx, lambda req: httpx.Response(200, json=data), product="chatgpt", auth="oauth"
    )
    s.providers.set_oauth(p.id, {"access_token": "token", "expires_at": 9999999999})
    result = ProviderUsageService(s.providers).get(p.id)
    assert [w["scope"] for w in result["windows"]] == ["default", "review"]
    assert [w["remaining_percent"] for w in result["windows"]] == [100, 0]
    assert result["balances"][0]["remaining"] == "-0.01"


def test_quota_exhaustion_invalidates_cached_snapshot(app_ctx):
    from rinari.providers.errors import ProviderError, ProviderErrorCode

    requests = []
    s, p = setup(
        app_ctx,
        lambda req: (
            requests.append(req)
            or httpx.Response(200, json={"usage": {"rolling": {"percent": 100}}})
        ),
    )
    now = [1790000000]
    usage = ProviderUsageService(s.providers, clock=lambda: now[0])
    usage.get(p.id)
    router = ModelRouter(s.providers, s.models, s.providers._client)

    def exhausted(secret):
        raise ProviderError("exhausted", code=ProviderErrorCode.RATE_LIMIT)

    with pytest.raises(ProviderError):
        router._authenticated_call(p, exhausted)
    now[0] += 6  # preserve the minimum backoff even after inference failure
    usage.get(p.id)
    assert len(requests) == 2


@pytest.mark.parametrize(
    "product,path",
    [
        ("openai", "/v1/chat/completions"),
        ("anthropic", "/v1/messages"),
        ("gemini", "/v1beta/openai/chat/completions"),
        ("xai", "/v1/chat/completions"),
        ("deepseek", "/v1/chat/completions"),
        ("mistral", "/v1/chat/completions"),
        ("openrouter", "/api/v1/chat/completions"),
        ("groq", "/openai/v1/chat/completions"),
        ("together", "/v1/chat/completions"),
        ("deepinfra", "/v1/openai/chat/completions"),
        ("fireworks", "/inference/v1/chat/completions"),
        ("zai", "/api/paas/v4/chat/completions"),
        ("zai-coding", "/api/coding/paas/v4/chat/completions"),
        ("moonshot", "/v1/chat/completions"),
        ("kimi-coding", "/coding/v1/chat/completions"),
        ("minimax", "/anthropic/v1/messages"),
        ("minimax-coding", "/anthropic/v1/messages"),
        ("ollama", "/v1/chat/completions"),
        ("lmstudio", "/v1/chat/completions"),
    ],
)
def test_catalog_product_routes_and_discovery(app_ctx, product, path):
    seen = []

    def handler(req):
        seen.append(req)
        if req.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "fixture"}]})
        if path.endswith("/messages"):
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    s, p = setup(
        app_ctx,
        handler,
        product=product,
        auth="none" if product in ("ollama", "lmstudio") else "api-key",
    )
    assert s.providers.test(p.id).connected
    assert len(seen) == 1
    m = s.models.add(p.id, "fixture", "fixture")
    response = ModelRouter(s.providers, s.models, s.providers._client).invoke(
        p, m.id, ModelRequest(model="fixture", messages=(ChatMessage.user("test"),))
    )
    assert response.content == "ok"
    assert seen[-1].url.path == path
    assert product_for(p) == product


def test_gemini_stream_signature_survives_tool_continuation(app_ctx):
    calls = []
    events = [
        {
            "choices": [
                {
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call",
                                "function": {"name": "read", "arguments": "{}"},
                                "extra_content": {"google": {"thought_signature": "opaque-signed"}},
                            }
                        ],
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]

    def handler(req):
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return httpx.Response(
                200,
                text="\n\n".join("data: " + json.dumps(e) for e in events) + "\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}
        )

    s, p = setup(app_ctx, handler, product="gemini")
    m = s.models.add(p.id, "gemini-3-pro-preview", "gemini")
    router = ModelRouter(s.providers, s.models, s.providers._client)
    req = ModelRequest(
        model=m.provider_model_id, messages=(ChatMessage.user("read"),), reasoning_effort="high"
    )
    visible = []
    response = router.invoke_stream(p, m.id, req, visible.append)
    assert visible == []
    follow = replace(
        req,
        messages=(
            *req.messages,
            ChatMessage.assistant(
                response.content, response.tool_calls, continuation=response.continuation
            ),
            ChatMessage.tool_result("call", "read", "ok"),
        ),
    )
    router.invoke(p, m.id, follow)
    assert (
        calls[1]["messages"][1]["tool_calls"][0]["extra_content"]["google"]["thought_signature"]
        == "opaque-signed"
    )
    assert calls[0]["reasoning_effort"] == "high"
    with pytest.raises(InvalidUsageError):
        router.invoke(p, m.id, replace(req, reasoning_effort="medium"))


@pytest.mark.parametrize("endpoint", ["/v1/messages", "/responses", "/chat/completions"])
def test_copilot_discovery_selects_route_and_subscription_headers(app_ctx, endpoint):
    seen = []

    def handler(req):
        seen.append(req)
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "claude-sonnet-4.6",
                            "supported_endpoints": [endpoint],
                            "capabilities": {
                                "supports": {"tool_calls": True, "vision": True},
                                "limits": {"max_context_window_tokens": 200000},
                            },
                        }
                    ]
                },
            )
        if endpoint == "/v1/messages":
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
            )
        if endpoint == "/responses":
            return httpx.Response(200, json={"status": "completed", "output": []})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    s, p = setup(app_ctx, handler, product="github-copilot", auth="oauth")
    s.providers.set_oauth(p.id, {"access_token": "copilot-token", "expires_at": 9999999999})
    discovered = s.providers.test(p.id).models[0]
    m = s.models.add(
        p.id, discovered.provider_model_id, "copilot", capabilities=discovered.capabilities
    )
    router = ModelRouter(s.providers, s.models, s.providers._client)
    router.invoke(
        p,
        m.id,
        ModelRequest(
            model=m.provider_model_id,
            messages=(ChatMessage.user("hello"),),
            session_id="test-session",
        ),
    )
    assert seen[-1].url.path == endpoint
    assert seen[-1].headers["authorization"] == "Bearer copilot-token"
    assert "x-api-key" not in seen[-1].headers
    assert seen[-1].headers["x-initiator"] == "user"
    assert seen[-1].headers["x-interaction-id"] == "test-session"
    assert router.capabilities(p, m.id).max_context_tokens == 200000
