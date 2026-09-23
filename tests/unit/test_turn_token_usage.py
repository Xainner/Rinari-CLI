from rinari.engine_protocol.token_usage import TurnTokenTracker
from rinari.models.types import ChatMessage, ModelRequest, ModelResponse, Usage
from rinari.models.usage_tracking import observe_call


def test_estimates_throttle_but_snapshot_keeps_latest_and_terminal_flushes():
    now = [0.0]
    tracker = TurnTokenTracker(clock=lambda: now[0])
    first = tracker.observe("usage.call.started", {"call_id": "a", "input_tokens": 10})
    assert first["total_tokens"] == 10
    for i in range(1, 100):
        now[0] = i / 1000
        assert tracker.observe("usage.call.delta", {"call_id": "a", "output_chars": i * 4}) is None
    assert tracker.last["total_tokens"] == 109
    terminal = tracker.finish()
    assert terminal["phase"] == "settled"
    assert terminal["source"] == "estimated"
    assert tracker.observe("usage.call.completed", {"call_id": "a"}) is None


def test_report_replaces_estimate_and_details_are_not_double_counted():
    tracker = TurnTokenTracker()
    tracker.observe("usage.call.started", {"call_id": "a", "input_tokens": 500})
    update = tracker.observe(
        "usage.call.completed",
        {
            "call_id": "a",
            "output_chars": 100,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cached_input_tokens": 8,
                "reasoning_tokens": 3,
                "source": "complete",
            },
        },
    )
    assert update["total_tokens"] == 15
    assert update["source"] == "reported"
    assert update["model_calls"] == 1
    assert update["cached_input_tokens"] == 8
    tracker.observe("usage.call.started", {"call_id": "child-independent", "input_tokens": 100})
    update = tracker.finish()
    assert update["source"] == "mixed"
    assert update["total_tokens"] == 115
    assert update["model_calls"] == 2


def test_retry_replaces_input_and_partial_usage_preserves_missing_estimate():
    tracker = TurnTokenTracker()
    tracker.observe("usage.call.started", {"call_id": "a", "input_tokens": 500})
    tracker.observe("usage.call.started", {"call_id": "a", "input_tokens": 50})
    update = tracker.observe(
        "usage.call.completed",
        {"call_id": "a", "output_chars": 40, "usage": {"output_tokens": 7, "source": "partial"}},
    )
    assert update["input_tokens"] == 50
    assert update["total_tokens"] == 57
    assert update["source"] == "mixed"
    assert update["model_calls"] == 1


def test_nested_router_observation_does_not_double_count_or_expose_content():
    tracker = TurnTokenTracker()
    events = []

    def sink(event, payload):
        events.append(payload)
        tracker.observe(event, payload)

    request = ModelRequest(
        model="private-model", messages=(ChatMessage.user("private prompt"),), usage_observer=sink
    )

    def adapter(delta):
        delta("private output")
        return ModelResponse("private output", usage=Usage(input_tokens=12, output_tokens=4))

    response = observe_call(
        request, lambda delta: observe_call(request, adapter, delta), lambda _: None
    )
    assert response.content == "private output"
    assert tracker.finish()["total_tokens"] == 16
    assert tracker.last["model_calls"] == 1
    assert "private" not in str(events)


def test_legacy_response_without_usage_retains_estimate():
    tracker = TurnTokenTracker()
    request = ModelRequest(
        model="fake", messages=(ChatMessage.user("hello"),), usage_observer=tracker.observe
    )
    response = observe_call(request, lambda _: ModelResponse("a response", usage=None))
    assert response.content == "a response"
    assert tracker.finish()["source"] == "estimated"


def test_unreported_completions_do_not_bypass_estimate_throttle():
    tracker = TurnTokenTracker(clock=lambda: 0)
    tracker.observe("usage.call.started", {"call_id": "a", "input_tokens": 10})
    assert tracker.observe("usage.call.completed", {"call_id": "a", "output_chars": 40}) is None
    assert tracker.last["total_tokens"] == 20
