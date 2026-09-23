"""Normalize provider context contracts without treating output caps as input windows."""

import contextlib
import hashlib
import json
import time


def normalize(metadata):
    if not isinstance(metadata, dict):
        return {}

    def positive(*keys):
        values = [metadata.get(key) for key in keys]
        return min((v for v in values if type(v) is int and v > 0), default=None)

    total = positive("max_context_tokens", "context_length", "context_window", "max_model_len")
    incoming = positive("max_input_tokens", "max_input_length")
    outgoing = positive("max_output_tokens", "max_completion_tokens")
    result = {}
    if total:
        result["max_context_tokens"] = total
    if incoming:
        result["max_input_tokens"] = incoming
    if outgoing:
        result["max_output_tokens"] = outgoing
    return result


def discover(ctx, main):
    """Cache scoped to the exact saved destination, never to model name alone."""
    if not hasattr(main, "router"):
        return {}
    model = ctx.model_repo.get(main.model_id)
    if model is None:
        return {}
    provider = main.provider
    key = hashlib.sha256(
        json.dumps([provider.id, provider.endpoint, model.provider_model_id]).encode()
    ).hexdigest()
    path = ctx.home / f"context-window-{key}.json"
    cached = {}
    if path.exists():
        with contextlib.suppress(ValueError, OSError):
            cached = json.loads(path.read_text(encoding="utf-8"))
    if cached.get("expires", 0) > time.time():
        return cached.get("limits", {})
    limits = {}
    try:
        adapter = main.router.adapter(provider)
        for item in adapter.list_models(
            main.router._providers.resolve_secret(provider), provider.endpoint
        ):
            if item.provider_model_id == model.provider_model_id:
                limits = normalize(item.capabilities)
                break
    except Exception:
        # Discovery is optional; authenticated invocation remains authoritative.
        limits = normalize(model.capabilities)
    if not limits:
        limits = normalize(model.capabilities)
    from rinari.providers.metadata import effective_metadata

    limits = {
        **normalize(effective_metadata(provider, model)),
        **limits,
        **normalize(model.capabilities),
    }
    with contextlib.suppress(OSError):
        path.write_text(
            json.dumps({"limits": limits, "expires": time.time() + (3600 if limits else 60)}),
            encoding="utf-8",
        )
    return limits
