"""One consumption base for preflight, `context.status` and the CLI meter.

The preflight that decides compaction, the status the desktop shows and the
CLI status line all measure through these functions, so they cannot disagree
about how full the context is or where compaction starts.
"""

from __future__ import annotations

from rinari.context.settings import input_budget, output_reserve
from rinari.context.tokens import estimate_tokens
from rinari.models.types import ModelRequest


def request_size(request):
    return estimate_tokens(history=request.messages, tools=request.tools)


def measure(request, anchor, model_ref):
    """Estimated input tokens of `request`, calibrated by the last reported usage.

    The anchor is the provider-reported input of the previous call on the same
    model and projection, next to what was estimated for it; the difference
    corrects the estimate. Returns ``(tokens, source)``.
    """
    used = request_size(request)
    anchor = anchor or {}
    calibrated = (
        anchor.get("model") == model_ref
        and type(anchor.get("actual")) is int
        and type(anchor.get("estimated")) is int
    )
    if not calibrated:
        return used, "estimated"
    return max(used, anchor["actual"] + used - anchor["estimated"]), "provider_anchored_estimate"


def thresholds(usable, compact_at_percent):
    """Where compaction starts and what it aims for, in tokens of usable input."""
    threshold = compact_at_percent / 100
    return {
        "compact_at_percent": compact_at_percent,
        "compact_at_tokens": int(usable * threshold),
        "target_tokens": int(usable * min(0.60, threshold * 0.75)),
    }


def limits(ctx, caller, resolved, request=None):
    """Usable input, the output reserved for it, and the compaction points."""
    request = request if request is not None else ModelRequest(model="", messages=())
    usable = input_budget(ctx, caller, request, resolved)
    return {
        "usable_input_tokens": usable,
        "output_reserve_tokens": output_reserve(ctx, caller, request) or 0,
        **thresholds(usable, ctx.config.config.context.compact_at_percent),
    }
