from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from rinari.application.vision import VisionCaller, configure, settings
from rinari.artifacts.store import ArtifactStore
from rinari.artifacts.transfer import import_file
from rinari.models.images import references
from rinari.models.types import (
    ChatMessage,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    Usage,
)
from rinari.runtime.cancellation import CancellationToken
from rinari.runtime.model_caller import ModelCaller


@pytest.fixture
def visual(app_ctx, tmp_path):
    provider = SimpleNamespace(id="provider", alias="test")
    model = SimpleNamespace(id="visual", provider_id="provider")
    calls = []

    class Router:
        main_vision = False
        cancel = None

        def capabilities(self, provider, model_id):
            return ProviderCapabilities(vision=True if model_id == "visual" else self.main_vision)

        def invoke(self, provider, model_id, request):
            if request.on_dispatched:
                request.on_dispatched()
            calls.append((model_id, request))
            if self.cancel and model_id == "visual":
                self.cancel.cancel()
            return ModelResponse(content="Descripción con ñ y tildes", usage=Usage(10, 5))

        def invoke_stream(self, provider, model_id, request, delta):
            result = self.invoke(provider, model_id, request)
            delta(result.content)
            return result

    services = SimpleNamespace(
        ctx=app_ctx,
        artifacts=ArtifactStore(app_ctx),
        models=SimpleNamespace(resolve=lambda ref: model),
        providers=SimpleNamespace(get=lambda ref: provider),
    )
    router = Router()
    caller = VisionCaller(
        services,
        SimpleNamespace(id="ses_vision", provider_id="provider", model_id="main"),
        ModelCaller(router, provider, "main"),
    )
    source = tmp_path / "imagen con ñ.png"
    Image.new("RGB", (30, 20), "purple").save(source)
    imported = import_file(services.artifacts, "ses_vision", source)
    refs = references(
        services.artifacts, "ses_vision", [{"uri": imported.uri(), "sha256": imported.sha256}]
    )
    source.unlink()
    request = ModelRequest(
        model="main",
        session_id="ses_vision",
        messages=(ChatMessage(role="user", content="¿Qué color tiene?", images=refs),),
    )
    # Configuration API is tested separately; avoid any provider service/network here.
    (app_ctx.home / "vision.json").write_text(
        '{"mode":"dedicated","model_id":"visual"}', encoding="utf-8"
    )
    return caller, request, calls


@pytest.mark.parametrize("mode", ["conversation", "dedicated"])
def test_visual_history_over_four_reaches_provider_without_mutation(visual, tmp_path, mode):
    caller, request, calls = visual
    caller.main.router.main_vision = True
    (caller.services.ctx.home / "vision.json").write_text(
        '{"mode":"' + mode + '","model_id":"visual"}', encoding="utf-8"
    )
    messages = []
    for n in range(7):
        source = tmp_path / f"history {n}.png"
        Image.new("RGB", (20, 20), (n * 30, 0, 0)).save(source)
        imported = import_file(caller.services.artifacts, "ses_vision", source)
        refs = references(
            caller.services.artifacts,
            "ses_vision",
            [{"uri": imported.uri(), "sha256": imported.sha256}],
        )
        messages.append(ChatMessage(role="user", content=f"Inspect {n}", images=refs))
    request = replace(request, messages=tuple(messages))
    caller.invoke(request)
    outgoing = [i.uri for m in calls[0][1].messages for i in m.images]
    assert outgoing == [
        m.images[0].uri for m in (messages if mode == "conversation" else messages[-1:])
    ]
    assert sum(len(m.images) for m in request.messages) == 7


def test_dedicated_pixels_only_reach_specialist_and_persisted_cache_is_question_specific(visual):
    caller, request, calls = visual
    events = []
    caller.activity_sink = lambda name, payload: events.append((name, payload))
    assert caller.capabilities().vision is False
    assert caller.visual_decision().available
    caller.invoke(request)
    assert [m for m, _ in calls] == ["visual", "main"]
    assert calls[0][1].messages[-1].images == request.messages[0].images
    assert all(not m.images for m in calls[1][1].messages)
    assert request.messages[0].images  # canonical history is unchanged
    assert "Descripción" in calls[1][1].messages[-1].content
    assert events[-1][0] == "vision.completed"
    caller.invoke(request)
    assert [m for m, _ in calls].count("visual") == 1
    assert len([e for e, _ in events if e == "vision.started"]) == 1
    caller.invoke(
        replace(request, messages=(replace(request.messages[0], content="¿Qué texto tiene?"),))
    )
    assert [m for m, _ in calls].count("visual") == 2


def test_automatic_prefers_explicit_auxiliary_even_when_main_has_vision(visual):
    caller, request, calls = visual
    caller.main.router.main_vision = True
    (caller.services.ctx.home / "vision.json").write_text(
        '{"mode":"automatic","model_id":"visual"}'
    )
    deltas = []
    caller.invoke_stream(request, deltas.append)
    assert [m for m, _ in calls] == ["visual", "main"]
    assert calls[0][1].messages[-1].images
    assert deltas


def test_cancel_during_specialist_never_calls_main_or_persists_analysis(visual):
    caller, request, calls = visual
    caller.token = CancellationToken()
    caller.main.router.cancel = caller.token
    with pytest.raises(Exception, match="cancelled"):
        caller.invoke(request)
    assert [m for m, _ in calls] == ["visual"]
    assert not [
        a
        for a in caller.services.artifacts.list(session_id="ses_vision")
        if a.namespace == "derived"
    ]


def test_conversation_mode_does_not_override_missing_vision(visual):
    caller, _request, calls = visual
    configure(caller.services, {"mode": "conversation"})
    assert settings(caller.services)["model_id"] is None
    assert caller.capabilities().vision is False
    assert calls == []


def test_dedicated_configuration_requires_model(visual):
    caller, _, _ = visual
    with pytest.raises(ValueError, match="Select"):
        configure(caller.services, {"mode": "dedicated"})


def test_image_hash_checked_before_cached_analysis(visual):
    caller, request, _ = visual
    caller.invoke(request)
    request.messages[0].images[0].path.write_bytes(b"changed")
    with pytest.raises(ValueError):
        caller.invoke(request)


def test_automatic_false_without_auxiliary_does_not_send_pixels(visual):
    caller, request, calls = visual
    configure(caller.services, {"mode": "automatic"})
    caller.main.router.main_vision = False
    with pytest.raises(ValueError, match="Vision settings"):
        caller.invoke(request)
    assert calls == []


def test_visual_usage_is_counted_without_tool_calls_or_invented_price(visual):
    from rinari.runtime.budget import BudgetMeter, TurnBudgetLimits
    from rinari.shared.clock import SystemClock

    caller, request, _calls = visual
    budget = BudgetMeter(TurnBudgetLimits(), SystemClock())
    caller.budget_getter = lambda: budget
    caller.invoke(request)
    assert budget.model_calls == 1  # Main is counted by AgentLoop, not the wrapper.
    assert (budget.input_tokens, budget.output_tokens) == (10, 5)
    assert budget.tool_calls == 0
    assert budget.estimated_cost() is None


def test_budget_exhaustion_prevents_auxiliary_network(visual):
    from rinari.runtime.budget import BudgetMeter, TurnBudgetLimits
    from rinari.shared.clock import SystemClock

    caller, request, calls = visual
    budget = BudgetMeter(TurnBudgetLimits(max_model_calls=1), SystemClock())
    budget.note_model_call()  # AgentLoop already reserved the main invocation.
    caller.budget_getter = lambda: budget
    with pytest.raises(ValueError, match="budget"):
        caller.invoke(request)
    assert calls == []


def test_real_router_and_adapter_transmit_visual_content_to_selected_model(visual):
    import json

    import httpx

    from rinari.application.provider_service import AddProviderInput
    from rinari.application.services import build_services
    from rinari.models.router import ModelRouter

    original, request, _ = visual
    services = build_services(original.services.ctx)
    services.providers.add(
        AddProviderInput(
            alias="visual-test",
            provider_type="openai",
            endpoint="https://visual.invalid/v1",
            secret="synthetic-test-secret",
        )
    )
    provider = services.providers.get("visual-test")
    main = services.models.add(
        provider.id,
        "text-model",
        "text",
        capabilities={"vision": False},
        settings={"transport": "chat"},
    )
    auxiliary = services.models.add(
        provider.id,
        "image-model",
        "images",
        capabilities={"vision": True},
        settings={"transport": "chat"},
    )
    configure(services, {"mode": "dedicated", "model_id": auxiliary.id})
    wire = []

    def respond(request):
        wire.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Análisis visual"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        router = ModelRouter(services.providers, services.models, client)
        caller = VisionCaller(services, original.record, ModelCaller(router, provider, main.id))
        caller.invoke(request)
    assert [r["model"] for r in wire] == ["image-model", "text-model"]
    visual_blocks = wire[0]["messages"][-1]["content"]
    assert any(
        b.get("type") == "image_url" and b["image_url"]["url"].startswith("data:image/")
        for b in visual_blocks
    )
    assert "data:image/" not in json.dumps(wire[1])
    assert "Análisis visual" in json.dumps(wire[1], ensure_ascii=False)


def test_unknown_requires_auxiliary_or_explicit_native_configuration(visual):
    caller, request, calls = visual
    (caller.services.ctx.home / "vision.json").unlink()
    caller.main.router.main_vision = None
    with pytest.raises(ValueError, match="Vision settings"):
        caller.invoke(request)
    assert calls == []
    configure(caller.services, {"mode": "automatic", "model_overrides": {"main": True}})
    caller.invoke(request)
    assert [m for m, _ in calls] == ["main"]


@pytest.mark.parametrize(
    "mode,main,dedicated,route,fallback",
    [
        ("automatic", None, False, "unavailable", False),
        ("automatic", True, True, "dedicated", False),
        ("automatic", None, True, "dedicated", False),
        ("automatic", False, True, "dedicated", False),
        ("automatic", False, False, "unavailable", False),
        ("dedicated", True, False, "unavailable", False),
        ("dedicated", None, True, "dedicated", False),
        ("conversation", None, True, "conversation", False),
        ("conversation", False, True, "conversation", False),
    ],
)
def test_visual_policy_matrix(mode, main, dedicated, route, fallback):
    from rinari.runtime.vision import resolve_visual_route

    decision = resolve_visual_route(mode, main, dedicated)
    assert (decision.route, decision.fallback) == (route, fallback)


@pytest.mark.parametrize(
    "status,message,unsupported",
    [
        (400, "This model does not support image inputs", True),
        (422, "Image inputs are not supported", True),
        (400, "Invalid image data", False),
        (400, "Invalid parameter temperature", False),
        (401, "This model does not support image inputs", False),
        (429, "This model does not support image inputs", False),
        (500, "This model does not support image inputs", False),
    ],
)
def test_provider_rejection_classification(status, message, unsupported):
    import httpx

    from rinari.providers.errors import ProviderErrorCode, classify_http_error

    error = classify_http_error(
        httpx.Response(status, json={"error": {"message": message}}), "https://example.invalid"
    )
    assert (error.error_code == ProviderErrorCode.VISION_UNSUPPORTED) is unsupported


def test_replay_uses_message_derivative_without_new_auxiliary_activity(visual):
    caller, request, calls = visual
    events = []
    caller.activity_sink = lambda e, p: events.append((e, p))
    caller.invoke(request)
    first = len(events)
    replay = replace(request, messages=(*request.messages, ChatMessage.user("Follow up")))
    caller.invoke(replay)
    assert [m for m, _ in calls] == ["visual", "main", "main"]
    assert len(events) == first
    # A new caller represents reload and still finds the immutable derivative.
    restored = VisionCaller(caller.services, caller.record, caller.main)
    restored.invoke(replay)
    assert [m for m, _ in calls].count("visual") == 1
    assert all(not m.images for m in calls[-1][1].messages)
    assert "Descripción" in calls[-1][1].messages[0].content


@pytest.mark.parametrize("code", ["AUTH", "RATE_LIMIT", "TIMEOUT", "SERVER_ERROR"])
def test_other_failures_do_not_fallback(visual, code):
    import json

    from rinari.providers.errors import ProviderError, ProviderErrorCode

    caller, request, calls = visual
    caller.main.router.main_vision = None
    (caller.services.ctx.home / "vision.json").write_text(
        json.dumps({"mode": "conversation", "model_id": "visual"})
    )

    def fail(provider, model, req):
        calls.append((model, req))
        raise ProviderError("Synthetic failure", code=ProviderErrorCode(code))

    caller.main.router.invoke = fail
    with pytest.raises(ProviderError):
        caller.invoke(request)
    assert [m for m, _ in calls] == ["main"]
    assert caller.visual_decision().route == "conversation"


def test_native_rejection_is_reported_without_silent_route_change(visual):
    from rinari.providers.errors import ProviderError, ProviderErrorCode

    caller, request, calls = visual
    configure(caller.services, {"mode": "conversation"})

    def fail(provider, model, req):
        calls.append((model, req))
        raise ProviderError("No image support", code=ProviderErrorCode.VISION_UNSUPPORTED)

    caller.main.router.invoke = fail
    with pytest.raises(ProviderError):
        caller.invoke(request)
    assert len(calls) == 1
    assert caller.visual_decision().route == "conversation"


def test_no_fallback_after_partial_stream(visual):
    import json

    from rinari.providers.errors import ProviderError, ProviderErrorCode

    caller, request, calls = visual
    caller.main.router.main_vision = None
    (caller.services.ctx.home / "vision.json").write_text(
        json.dumps({"mode": "conversation", "model_id": "visual"})
    )

    def fail(provider, model, req, delta):
        calls.append((model, req))
        delta("Partial output")
        raise ProviderError("Unsupported images", code=ProviderErrorCode.VISION_UNSUPPORTED)

    caller.main.router.invoke_stream = fail
    with pytest.raises(ProviderError):
        caller.invoke_stream(request, lambda text: None)
    assert len(calls) == 1


def test_new_attachment_analyzes_only_its_message_and_clean_question(visual):
    caller, request, calls = visual
    caller.invoke(request)
    current = replace(
        request.messages[0],
        content="internal attachment metadata",
        display_content="Mira esta otra",
    )
    events = []
    caller.activity_sink = lambda e, p: events.append((e, p))
    caller.invoke(
        replace(request, messages=(*request.messages, ChatMessage.assistant("answer"), current))
    )
    assert [m for m, _ in calls] == ["visual", "main", "visual", "main"]
    assert len(calls[2][1].messages[-1].images) == 1
    assert calls[2][1].messages[-1].content == "Mira esta otra"
    assert next(p for e, p in events if e == "vision.started")["question"] == "Mira esta otra"


def test_model_switch_reuses_historical_auxiliary_observation(visual):
    caller, request, calls = visual
    caller.invoke(request)
    configure(caller.services, {"mode": "conversation"})
    caller.invoke(replace(request, messages=(*request.messages, ChatMessage.user("Continue"))))
    assert [m for m, _ in calls] == ["visual", "main", "main"]
    assert all(not m.images for m in calls[-1][1].messages)
    assert "Descripción" in calls[-1][1].messages[0].content


def test_missing_historical_derivative_does_not_trigger_analysis(visual):
    caller, request, calls = visual
    caller.invoke(replace(request, messages=(*request.messages, ChatMessage.user("Continue"))))
    assert [m for m, _ in calls] == ["main"]
    assert "fs.read_image" in calls[-1][1].messages[0].content


def test_compacted_auxiliary_attachment_keeps_observation(visual):
    from rinari.models.visual_context import select_visual_context

    caller, request, calls = visual
    caller.invoke(request)
    next_image = replace(request.messages[0], content="A new question")
    history = replace(request, messages=(*request.messages, next_image))
    caller.invoke(history)
    count = len(calls)
    compacted = select_visual_context(history, compact=True)
    caller.invoke(replace(compacted, messages=(*compacted.messages, ChatMessage.user("Follow up"))))
    assert len(calls) == count + 1
    assert "Descripción" in calls[-1][1].messages[0].content


def test_model_override_can_be_removed(visual):
    caller, _request, _calls = visual
    caller.main.router.main_vision = None
    configure(caller.services, {"mode": "automatic", "model_overrides": {"main": True}})
    assert caller.visual_decision().route == "conversation"
    configure(caller.services, {"mode": "automatic", "model_overrides": {"main": False}})
    assert not caller.visual_decision().available
    configure(caller.services, {"mode": "automatic", "model_overrides": {}})
    assert not caller.visual_decision().available
    with pytest.raises(ValueError):
        configure(caller.services, {"model_overrides": {"main": "false"}})


def test_visual_byte_limits_are_explicit_and_do_not_drop_images(visual):
    from rinari.models.images import validate_visual_payload

    _caller, request, _calls = visual
    with pytest.raises(ValueError, match="No images sent"):
        validate_visual_payload(request, {"max_encoded_bytes": 1})
    many = replace(
        request, messages=(replace(request.messages[0], images=request.messages[0].images * 7),)
    )
    validate_visual_payload(many)
    with pytest.raises(ValueError, match="requested 7"):
        validate_visual_payload(many, {"max_images": 6})


def test_internal_nudge_does_not_turn_new_tool_image_into_history(visual):
    from rinari.models.types import ToolCall

    caller, request, calls = visual
    owner = replace(request.messages[0], display_content=request.messages[0].content)
    caller.invoke(replace(request, messages=(owner,)))
    tool = ChatMessage(role="tool", tool_call_id="inspect", images=owner.images, content="loaded")
    messages = (
        owner,
        ChatMessage.assistant(
            "", (ToolCall("inspect", "fs.read_image", {"question": "Inspect the text"}),)
        ),
        tool,
        ChatMessage.user("Internal recovery guidance"),
    )
    caller.invoke(replace(request, messages=messages))
    assert [m for m, _ in calls] == ["visual", "main", "visual", "main"]
    assert calls[2][1].messages[-1].content == "Inspect the text"


def test_retired_projection_still_persists_original_image_references(visual):
    from rinari.cli.agent_runtime import _message_to_record
    from rinari.models.visual_context import retire_images

    caller, request, _calls = visual
    original = request.messages[0]
    retired = retire_images(original)
    record = _message_to_record(caller.services, "ses_vision", retired, "2026-09-12T00:00:00Z")
    assert record.images == [{"uri": original.images[0].uri, "sha256": original.images[0].sha256}]


def test_transport_pressure_retires_history_but_never_trims_current_batch(visual):
    from rinari.models.images import VisualPayloadLimitError
    from rinari.models.visual_context import prepare_visual_payload

    _caller, request, _calls = visual
    old = replace(request.messages[0], images=request.messages[0].images * 5)
    current = replace(request.messages[0], content="current", images=request.messages[0].images * 2)
    combined = replace(request, messages=(old, current))
    projected = prepare_visual_payload(combined, {"max_images": 3})
    assert not projected.messages[0].images
    assert projected.messages[1].images == current.images
    assert combined.messages[0].images == old.images
    with pytest.raises(VisualPayloadLimitError):
        prepare_visual_payload(combined, {"max_images": 1})


def test_auxiliary_batch_concurrent_preserves_order_and_failures(visual):
    import threading

    from rinari.models.types import ToolCall

    caller, request, calls = visual
    barrier = threading.Barrier(3)
    original = caller.main.router.invoke

    def invoke(provider, model_id, req):
        if model_id == "visual":
            req.on_dispatched()
            barrier.wait(timeout=3)
            question = req.messages[-1].content
            if question == "bad":
                raise ValueError("provider rejected this image")
            return ModelResponse(content=question)
        return original(provider, model_id, req)

    caller.main.router.invoke = invoke
    toolcalls = tuple(
        ToolCall(id=str(i), name="fs.read_image", arguments={"question": q})
        for i, q in enumerate(("first", "second", "bad"))
    )
    req = replace(
        request,
        messages=(
            ChatMessage.user("inspect"),
            ChatMessage(role="assistant", content="", tool_calls=toolcalls),
            *(
                ChatMessage(
                    role="tool",
                    content="loaded",
                    tool_call_id=c.id,
                    images=request.messages[0].images,
                )
                for c in toolcalls
            ),
        ),
    )
    with pytest.raises(ValueError, match="provider rejected"):
        caller.invoke(req)
    rows = caller.services.ctx.db.query("select name from artifacts where namespace='derived'")
    assert len(rows) == 2
    assert not calls  # Main model never receives an incomplete batch as if successful.
    # Retry explicitly: successful siblings use cache; only failed query executes.
    seen = []

    def retry(provider, model_id, req):
        if model_id == "visual":
            req.on_dispatched()
            seen.append(req.messages[-1].content)
            return ModelResponse(content="third")
        calls.append((model_id, req))
        return ModelResponse(content="done")

    caller.main.router.invoke = retry
    caller.invoke(req)
    assert seen == ["bad"]
    assert [m.content.split("\n")[-1] for m in calls[-1][1].messages[-3:]] == [
        "first",
        "second",
        "third",
    ]


def test_auxiliary_duplicate_queries_share_request_and_preserve_origins(visual):
    from rinari.models.types import ToolCall

    caller, request, calls = visual
    tc = tuple(
        ToolCall(id=str(i), name="fs.read_image", arguments={"question": "same"}) for i in range(3)
    )
    req = replace(
        request,
        messages=(
            ChatMessage.user("inspect"),
            ChatMessage(role="assistant", content="", tool_calls=tc),
            *(
                ChatMessage(
                    role="tool",
                    content="loaded",
                    tool_call_id=c.id,
                    images=request.messages[0].images,
                )
                for c in tc
            ),
        ),
    )
    caller.invoke(req)
    assert len([c for c in calls if c[0] == "visual"]) == 1
    assert (
        len(caller.services.ctx.db.query("select name from artifacts where namespace='derived'"))
        == 3
    )


def test_auxiliary_partial_output_is_not_silently_shortened_or_completed(visual):
    from rinari.models.types import StopReason

    caller, request, calls = visual
    original = caller.main.router.invoke

    def invoke(provider, model_id, req):
        if model_id == "visual":
            assert req.max_tokens is None
            req.on_dispatched()
            return ModelResponse(content="z" * 17000, stop_reason=StopReason.MAX_TOKENS)
        return original(provider, model_id, req)

    caller.main.router.invoke = invoke
    events = []
    caller.activity_sink = lambda e, p: events.append((e, p))
    caller.invoke(request)
    assert events[-1][0] == "vision.partial"
    assert "PARTIAL" in calls[-1][1].messages[-1].content
    assert "z" * 17000 in calls[-1][1].messages[-1].content
    caller.invoke(request)
    assert "PARTIAL" in calls[-1][1].messages[-1].content


def test_visual_payload_output_policy_matches_adapter_contracts(visual):
    from rinari.providers.adapters.anthropic import AnthropicAdapter
    from rinari.providers.adapters.openai_compatible import OpenAICompatibleAdapter
    from rinari.providers.adapters.responses import OpenAIResponsesAdapter

    _caller, request, _ = visual
    adapter = OpenAICompatibleAdapter("https://example.test/v1")
    for streaming in (False, True):
        body = adapter._payload(request, stream=streaming)
        assert "max_tokens" not in body
        assert body["messages"][0]["content"][1]["type"] == "image_url"
        body = OpenAIResponsesAdapter()._responses_payload(request, stream=streaming)
        assert "max_output_tokens" not in body
        assert "image" in str(body)
        configured = replace(request, max_tokens=32000)
        assert adapter._payload(configured, stream=streaming)["max_tokens"] == 32000
        assert (
            OpenAIResponsesAdapter()._responses_payload(configured, stream=streaming)[
                "max_output_tokens"
            ]
            == 32000
        )
        anthropic = AnthropicAdapter()
        assert (
            anthropic._payload(request, stream=streaming)["max_tokens"]
            == anthropic.default_max_tokens
        )
        assert anthropic._payload(configured, stream=streaming)["max_tokens"] == 32000
