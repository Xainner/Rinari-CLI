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


def test_call_boundaries_bypass_the_delta_throttle():
    # A tool that takes 30 ms: the next call starts inside the throttle window
    # of the previous completion. Its input estimate has to reach the UI now,
    # not at its first delta (or its end, if it does not stream).
    now = [0.0]
    tracker = TurnTokenTracker(clock=lambda: now[0])
    tracker.observe("usage.call.started", {"call_id": "a", "input_tokens": 11000})
    now[0] = 2.0
    usage = {"input_tokens": 11000, "output_tokens": 500, "source": "complete"}
    tracker.observe("usage.call.completed", {"call_id": "a", "usage": usage})
    now[0] = 2.03
    started = tracker.observe("usage.call.started", {"call_id": "b", "input_tokens": 12000})
    assert started["total_tokens"] == 23500
    assert started["model_calls"] == 2
    assert started["source"] == "mixed"
    # A completion without reported usage still carries the output estimate.
    now[0] = 2.04
    completed = tracker.observe("usage.call.completed", {"call_id": "b", "output_chars": 40})
    assert completed["total_tokens"] == 23510
    # Deltas stay throttled.
    now[0] = 2.05
    tracker.observe("usage.call.started", {"call_id": "c", "input_tokens": 1})
    now[0] = 2.06
    assert tracker.observe("usage.call.delta", {"call_id": "c", "output_chars": 400}) is None
    assert tracker.last["total_tokens"] == 23611


def test_every_request_gets_its_own_call_key():
    # Agents reuse model_call_id values such as "model_1"; the tracker keys
    # calls by an opaque id minted per request, which replace() preserves.
    from dataclasses import replace

    first = ModelRequest(model="m", messages=(ChatMessage.user("x"),))
    second = ModelRequest(model="m", messages=(ChatMessage.user("x"),))
    assert first.usage_call_id != second.usage_call_id
    assert replace(first, model="n").usage_call_id == first.usage_call_id


def test_context_is_the_last_main_call_while_the_total_is_the_cost():
    tracker = TurnTokenTracker()
    tracker.observe("usage.call.started", {"call_id": "a", "input_tokens": 9000})
    tracker.observe("usage.call.completed", {"call_id": "a", "output_chars": 400})
    # A tool round-trip resends the whole conversation: the cost adds up...
    tracker.observe("usage.call.started", {"call_id": "b", "input_tokens": 9300})
    # ...and a subagent's call is cost, not size of this conversation.
    tracker.observe(
        "usage.call.started", {"call_id": "c", "input_tokens": 2000, "agent_id": "agt_1"}
    )
    update = tracker.finish()
    assert update["total_tokens"] == 9000 + 100 + 9300 + 2000
    assert update["context_tokens"] == 9300


def test_a_cache_hiding_report_does_not_shrink_the_conversation():
    """xAInner reported 1 990 input tokens for a ~11 000-token prompt (prefix
    cache, no cached count): the cost is the report, the size is the prompt."""
    tracker = TurnTokenTracker()
    tracker.observe("usage.call.started", {"call_id": "a", "input_tokens": 10600})
    update = tracker.observe(
        "usage.call.completed",
        {
            "call_id": "a",
            "usage": {"input_tokens": 1990, "output_tokens": 22, "source": "complete"},
        },
    )
    assert update["total_tokens"] == 2012
    assert update["context_tokens"] == 10600 + 22
