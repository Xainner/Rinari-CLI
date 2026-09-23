"""Effective context capacity of one saved destination, with provenance.

Every limit (total window, input, output) is resolved on its own, from the
first source that states it:

    explicit override  →  endpoint  →  catalog

A manual window from the context settings outranks all of them and skips
discovery; when nothing states a window, the caller falls back to an
estimate. Each result says where it came from, so a catalog value or a
fallback is never shown as something the endpoint reported.
"""

import contextlib
import hashlib
import json
import threading
import time
from datetime import UTC, datetime

DISCOVERY_TTL = 3600
# A failed discovery is remembered only to avoid hammering an offline
# endpoint. It is a failure, not data: nothing is confirmed from it.
FAILURE_TTL = 60
DIMENSIONS = (
    ("context", "max_context_tokens"),
    ("input", "max_input_tokens"),
    ("output", "max_output_tokens"),
)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


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


def _key(provider):
    """One discovery per endpoint: every saved model on it shares the answer."""
    return hashlib.sha256(json.dumps([provider.id, provider.endpoint or ""]).encode()).hexdigest()


def _path(ctx, provider):
    return ctx.home / f"context-discovery-{_key(provider)}.json"


def _lock(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _write(path, value):
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(value), encoding="utf-8")


def remember(ctx, provider, discovered):
    """Seed the cache with a discovery someone else already paid for."""
    _write(
        _path(ctx, provider),
        {
            "ok": True,
            "observed_at": datetime.now(UTC).isoformat(),
            "expires": time.time() + DISCOVERY_TTL,
            "models": {mid: normalize(caps) for mid, caps in discovered.items()},
        },
    )


def forget(ctx, provider=None):
    """Drop cached discoveries: one endpoint's, or all of them."""
    paths = [_path(ctx, provider)] if provider else ctx.home.glob("context-discovery-*.json")
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink()


def _discover(ctx, main, provider):
    path = _path(ctx, provider)
    with _lock(_key(provider)):
        cached = {}
        if path.exists():
            with contextlib.suppress(ValueError, OSError):
                cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("expires", 0) > time.time():
            return cached if cached.get("ok") else None
        try:
            adapter = main.router.adapter(provider)
            items = adapter.list_models(
                main.router._providers.resolve_secret(provider), provider.endpoint
            )
        except Exception:
            # Discovery is optional; authenticated invocation remains authoritative.
            _write(path, {"ok": False, "expires": time.time() + FAILURE_TTL})
            return None
        result = {
            "ok": True,
            "observed_at": datetime.now(UTC).isoformat(),
            "expires": time.time() + DISCOVERY_TTL,
            "models": {item.provider_model_id: normalize(item.capabilities) for item in items},
        }
        _write(path, result)
        return result


def endpoint_limits(ctx, main, model, provider):
    """What the endpoint states for this model, and when it said it."""
    discovery = _discover(ctx, main, provider)
    if discovery is not None:
        return discovery["models"].get(model.provider_model_id) or {}, discovery["observed_at"]
    # Offline: the last discovery saved for this destination (refresh or add).
    saved = (model.settings or {}).get("discovered_capabilities")
    return normalize(saved), (model.settings or {}).get("discovered_at")


def capacity(ctx, main, *, discover=True):
    """Per-limit value and source for the caller's saved destination.

    ``discover=False`` skips the network: a manual window does not need it.
    """
    empty = {"limits": {}, "sources": {}, "observed_at": None, "catalog_updated_at": None}
    if not hasattr(main, "router"):
        return empty
    model = ctx.model_repo.get(main.model_id)
    if model is None:
        return empty
    provider = main.provider
    from rinari.providers.metadata import model_metadata

    catalog = model_metadata(provider, model.provider_model_id)
    endpoint, observed_at = endpoint_limits(ctx, main, model, provider) if discover else ({}, None)
    layers = (
        ("override", normalize(model.capabilities)),
        ("provider", endpoint),
        ("catalog", normalize(catalog)),
    )
    limits, sources = {}, {}
    for name, key in DIMENSIONS:
        for source, values in layers:
            if values.get(key):
                limits[key] = values[key]
                sources[name] = source
                break
    return {
        "limits": limits,
        "sources": sources,
        "observed_at": observed_at if "provider" in sources.values() else None,
        "catalog_updated_at": catalog.get("updated_at") if "catalog" in sources.values() else None,
    }
