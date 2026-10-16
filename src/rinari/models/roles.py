"""Model roles beyond the conversation's own (plan 0.4.0 #80).

- **Fallback:** when the conversation's model fails after its retries (a
  provider error, no connection, a timeout, a limit) or returns two empty
  answers in a row, the turn goes on with this model from the same history,
  and says so. The next turn tries the conversation's model again. Never on a
  content refusal: that is not a technical failure.
- **Auxiliary:** a cheaper model for internal calls (titles today). Without
  it, the conversation's model, as before. Compaction uses it only when the
  owner turns that on.
"""

from __future__ import annotations

from typing import Any

from rinari.shared.clock import now_iso

KEY_FALLBACK = "models.fallback"
KEY_AUXILIARY = "models.auxiliary"
KEY_AUXILIARY_COMPACTION = "models.auxiliary_compaction"


def get_roles(ctx) -> dict[str, Any]:
    repo = ctx.config_repo
    return {
        "fallback": repo.get(KEY_FALLBACK) or None,
        "auxiliary": repo.get(KEY_AUXILIARY) or None,
        "auxiliary_compaction": repo.get(KEY_AUXILIARY_COMPACTION) == "true",
    }


def set_roles(ctx, models, **changes: Any) -> dict[str, Any]:
    """Set any of fallback, auxiliary (a model alias or id, None clears) and
    auxiliary_compaction (bool)."""
    from rinari.storage.records import ConfigValue

    stamp = now_iso(ctx.clock)
    for role, key in (("fallback", KEY_FALLBACK), ("auxiliary", KEY_AUXILIARY)):
        if role not in changes:
            continue
        ref = changes[role]
        if ref in (None, ""):
            ctx.config_repo.delete(key)
            continue
        model = models.resolve(str(ref))
        ctx.config_repo.set(ConfigValue(key=key, value=model.id, updated_at=stamp))
    if "auxiliary_compaction" in changes:
        value = "true" if changes["auxiliary_compaction"] else "false"
        ctx.config_repo.set(
            ConfigValue(key=KEY_AUXILIARY_COMPACTION, value=value, updated_at=stamp)
        )
    return get_roles(ctx)


def role_caller(ctx, router, role: str, *, exclude_model_id: str | None = None):
    """A ModelCaller for the model of `role`, or None (unset, gone, or the same model)."""
    from rinari.runtime.model_caller import ModelCaller

    model_id = get_roles(ctx).get(role)
    if not model_id or model_id == exclude_model_id:
        return None
    model = ctx.model_repo.get(model_id)
    if model is None:
        return None
    provider = ctx.provider_repo.get(model.provider_id)
    if provider is None:
        return None
    return ModelCaller(router, provider, model.id)


__all__ = ["KEY_AUXILIARY", "KEY_FALLBACK", "get_roles", "role_caller", "set_roles"]
