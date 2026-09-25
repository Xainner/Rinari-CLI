"""An aggregator upstream that refuses the reasoning control does not end the turn."""

import pytest

from rinari.models.router import rejects_reasoning_control, without_rejected_reasoning
from rinari.models.types import ChatMessage, ModelRequest, ModelResponse, StopReason
from rinari.shared.errors import ProviderModelError

URL = "https://opencode.ai/zen/go/v1/chat/completions"
REFUSED = ProviderModelError(
    f"Provider returned HTTP 400 for {URL}: Upstream request failed: "
    "[invalid_request_error] native reasoning control reasoning_effort is not allowed"
)


def _request(effort="medium", observer=None):
    return ModelRequest(
        model="glm-5.3-flash",
        messages=(ChatMessage.user("hola"),),
        reasoning_effort=effort,
        usage_observer=observer,
    )


def test_only_a_refusal_that_names_reasoning_qualifies() -> None:
    assert rejects_reasoning_control(REFUSED)
    assert rejects_reasoning_control(
        ProviderModelError(
            f"Provider returned HTTP 400 for {URL}: "
            "This model does not support configurable reasoning."
        )
    )
    assert not rejects_reasoning_control(
        ProviderModelError(
            f'Provider returned HTTP 400 for {URL}: messages[3]: "name" is not supported'
        )
    )
    assert not rejects_reasoning_control(
        ProviderModelError(f"Provider returned HTTP 500 for {URL}: reasoning backend not allowed")
    )


def test_the_same_call_repeats_once_without_the_control() -> None:
    seen: list = []
    events: list = []

    def call(request):
        seen.append(request.reasoning_effort)
        if request.reasoning_effort:
            raise REFUSED
        return ModelResponse(content="hola", stop_reason=StopReason.END_TURN)

    request = _request(observer=lambda name, payload: events.append((name, payload)))
    assert without_rejected_reasoning(request, call).content == "hola"
    assert seen == ["medium", None]
    assert events[0][0] == "provider.reasoning.dropped"
    assert events[0][1]["effort"] == "medium"


@pytest.mark.parametrize(
    ("effort", "partial", "started"),
    [(None, False, False), ("high", True, False), ("high", False, True)],
)
def test_it_does_not_retry_when_that_would_be_wrong(effort, partial, started) -> None:
    calls: list = []
    error = ProviderModelError(str(REFUSED), details={"partial": partial})

    def call(request):
        calls.append(request)
        raise error

    with pytest.raises(ProviderModelError):
        without_rejected_reasoning(_request(effort), call, output_started=lambda: started)
    assert len(calls) == 1


def test_any_other_error_is_left_alone() -> None:
    other = ProviderModelError(f"Provider returned HTTP 400 for {URL}: context too long")

    def call(request):
        raise other

    with pytest.raises(ProviderModelError) as caught:
        without_rejected_reasoning(_request(), call)
    assert caught.value is other
