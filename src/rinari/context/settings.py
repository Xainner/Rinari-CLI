"""Installation-owned context routing preferences; no provider credentials."""

import json
import os
import tempfile

from rinari.shared.errors import ConfigurationError


def load(ctx):
    path = ctx.home / "context-policy.json"
    defaults = {"model_id": None, "model_windows": {}}
    if path.exists():
        defaults.update(json.loads(path.read_text(encoding="utf-8")))
    defaults.update(
        enabled=ctx.config.config.runtime.safeguards.context_compaction,
        compact_at_percent=ctx.config.config.context.compact_at_percent,
    )
    return defaults


def save(services, value):
    from rinari.application.config.loader import EffectiveConfig, load_effective_config
    from rinari.application.config.writer import read_user_data, set_dotted, write_user_data

    if (
        type(value.get("enabled")) is not bool
        or type(value.get("compact_at_percent")) is not int
        or not 1 <= value["compact_at_percent"] <= 100
    ):
        raise ConfigurationError("Specify enabled and compact_at_percent (1-100).")
    model_id = value.get("model_id")
    if model_id:
        model_id = services.models.resolve(model_id).id
    windows = value.get("model_windows", {})
    if not isinstance(windows, dict):
        raise ConfigurationError("model_windows must map saved model IDs to context windows.")
    for ref, window in windows.items():
        services.models.resolve(ref)
        if type(window) is not int or window < 1:
            raise ConfigurationError("Context windows must be positive integers.")
    ctx = services.ctx
    data = read_user_data(ctx.layout)
    data = set_dotted(data, "runtime.safeguards.context_compaction", value["enabled"])
    data = set_dotted(data, "context.compact_at_percent", value["compact_at_percent"])
    write_user_data(ctx.layout, data)
    path = ctx.home / "context-policy.json"
    fd, temp = tempfile.mkstemp(dir=ctx.home, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"model_id": model_id, "model_windows": windows}, stream)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    project_layers = [layer for layer in ctx.config.layers if layer.name == "project"]
    ctx.config = EffectiveConfig([*load_effective_config(ctx.layout).layers, *project_layers])
    return load(ctx)


def destination(caller):
    while hasattr(caller, "current") or hasattr(caller, "main"):
        caller = caller.current if hasattr(caller, "current") else caller.main
    return caller


# Used only when nothing states a window. It is an estimate and is reported
# as one (`window_source: fallback`, `window_estimated: true`).
FALLBACK_WINDOW = 128_000


def window(ctx, caller):
    """Effective input window for the caller's destination, and its provenance.

    `window_source` names the source of the limit that decides the effective
    window: `manual` (context settings), `override` (saved on the model),
    `provider` (announced by the endpoint), `catalog` (bundled metadata, with
    `metadata_updated_at`) or `fallback`. Output limits are reported, never
    reserved here; the reservation is `input_budget`'s, from the request.
    """
    from rinari.context.windows import capacity

    main = destination(caller)
    manual = load(ctx)["model_windows"].get(getattr(main, "model_id", None))
    known = capacity(ctx, main, discover=not manual)
    limits, sources = known["limits"], dict(known["sources"])
    context = limits.get("max_context_tokens")
    if context is None and not manual:
        # A caller without a saved destination still declares its window.
        declared = caller.capabilities().max_context_tokens
        if declared:
            context, sources["context"] = declared, "provider"
    incoming = limits.get("max_input_tokens")
    if manual:
        effective, total, source = manual, manual, "manual"
        context, sources["context"] = manual, "manual"
    elif incoming and (context is None or incoming < context):
        total = context or max(incoming, FALLBACK_WINDOW)
        effective, source = incoming, sources["input"]
    elif context:
        effective, total, source = context, context, sources["context"]
    else:
        effective, total, source = FALLBACK_WINDOW, FALLBACK_WINDOW, "fallback"
    return {
        "window_tokens": effective,
        "total_window_tokens": total,
        "window_source": source,
        "window_estimated": source == "fallback",
        "max_context_tokens": context,
        "max_input_tokens": incoming,
        "max_output_tokens": limits.get("max_output_tokens"),
        "limit_sources": {name: sources.get(name) for name in ("context", "input", "output")},
        "discovered_at": known["observed_at"],
        "metadata_updated_at": known["catalog_updated_at"],
    }


def output_reserve(ctx, caller, request):
    """Output tokens the request reserves: its own, or the configured budget."""
    main = destination(caller)
    output = request.max_tokens
    if hasattr(main, "router"):
        model = ctx.model_repo.get(main.model_id)
        if model is not None:
            output = main.router.generation_request(main.provider, model, request).max_tokens
    return output


def input_budget(ctx, caller, request, resolved):
    output = output_reserve(ctx, caller, request)
    return min(resolved["window_tokens"], resolved["total_window_tokens"] - (output or 0))


def summarizer(ctx, caller):
    main = destination(caller)
    ref = load(ctx)["model_id"]
    if not ref:
        return main
    from rinari.runtime.model_caller import ModelCaller

    model = ctx.model_repo.get(ref)
    if model is None:
        raise ConfigurationError(
            "Configured context summarizer is unavailable. Select a model in Settings."
        )
    provider = ctx.provider_repo.get(model.provider_id)
    return ModelCaller(main.router, provider, model.id)
