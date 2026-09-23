"""Content-free, runtime-only observation of a logical model invocation."""

from dataclasses import asdict

from rinari.context.tokens import estimate_tokens


def observe_call(request, invoke, on_delta=None):
    sink = request.usage_observer
    if sink is None:
        return invoke(on_delta)
    identity = request.usage_call_id
    sink(
        "usage.call.started",
        {
            "call_id": identity,
            "input_tokens": estimate_tokens(history=request.messages, tools=request.tools),
        },
    )
    chars = 0

    def delta(text):
        nonlocal chars
        chars += len(text)
        sink("usage.call.delta", {"call_id": identity, "output_chars": chars})
        if on_delta is not None:
            on_delta(text)

    response = invoke(delta if on_delta is not None else None)
    sink(
        "usage.call.completed",
        {
            "call_id": identity,
            "usage": asdict(response.usage) if response.usage is not None else {},
            "output_chars": max(chars, len(response.content or "")),
        },
    )
    return response
