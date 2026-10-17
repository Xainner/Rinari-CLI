"""Model Registry Service (docs/commands.md sections 17-20, 72).

Models are saved per provider and resolved by alias, provider model ID,
or internal ID. `use` persists the selection in config_values and updates
the provider's default/last-used model (provider-specific model memory).
Refresh marks temporarily missing models unavailable instead of deleting
them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

import httpx

from rinari.application.context import AppContext
from rinari.application.provider_service import (
    KEY_ACTIVE_MODEL,
    KEY_ACTIVE_PROVIDER,
    ProviderService,
)
from rinari.providers.adapters.base import DiscoveredModel
from rinari.providers.registry import adapter_for
from rinari.shared.clock import now_iso
from rinari.shared.errors import ConflictError, InvalidUsageError, NotFoundError, RinariError
from rinari.storage.records import ConfigValue, ModelRecord, ProviderRecord


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    model: ModelRecord
    provider: ProviderRecord
    switched_provider: bool = False


@dataclass(frozen=True, slots=True)
class RefreshResult:
    saved: int
    still_available: int
    marked_unavailable: int
    discovered: int
    error: str | None = None
    #: Aliases saved by `refresh(add_new=True)` for models the provider started offering.
    added: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelTestResult:
    ok: bool
    detail: str
    model: ModelRecord
    provider: ProviderRecord


class ModelService:
    def __init__(
        self,
        ctx: AppContext,
        providers: ProviderService,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._ctx = ctx
        self._providers = providers
        self._client = http_client
        #: Outcome of the last public catalog download (``refresh(public_catalog=True)``).
        self.last_catalog_refresh: dict | None = None

    def _now(self) -> str:
        return now_iso(self._ctx.clock)

    # -- hidden models (plan 0.4.0 #93) -----------------------------------
    # What a provider offers is saved on its own. «Quitar» hides a model so the
    # next refresh does not bring it back; «Mostrar» saves it again.

    @staticmethod
    def _hidden_key(provider_id: str) -> str:
        return f"models.hidden.{provider_id}"

    def _hidden_ids(self, provider_id: str) -> list[str]:
        raw = self._ctx.config_repo.get(self._hidden_key(provider_id))
        try:
            value = json.loads(raw) if raw else []
        except ValueError:
            return []
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    def _set_hidden(self, provider_id: str, ids: list[str]) -> None:
        key = self._hidden_key(provider_id)
        if ids:
            self._ctx.config_repo.set(
                ConfigValue(key=key, value=json.dumps(sorted(set(ids))), updated_at=self._now())
            )
        else:
            self._ctx.config_repo.delete(key)

    def hidden(self, provider_ref: str) -> list[str]:
        """The provider model IDs the owner removed (they stay out of refreshes)."""
        return self._hidden_ids(self._providers.get(provider_ref).id)

    def unhide(self, provider_ref: str, provider_model_id: str) -> ModelRecord:
        """«Mostrar»: the hidden model is saved again under a free alias."""
        provider = self._providers.get(provider_ref)
        ids = self._hidden_ids(provider.id)
        if provider_model_id not in ids:
            raise NotFoundError(f"Model {provider_model_id!r} is not hidden on {provider.alias!r}")
        self._set_hidden(provider.id, [item for item in ids if item != provider_model_id])
        existing = self._ctx.model_repo.get_by_provider_model_id(provider.id, provider_model_id)
        if existing is not None:
            return existing
        return self.add(
            provider.id, provider_model_id, self._free_alias(provider.id, provider_model_id)
        )

    def save_discovered(self, found: dict[str, list[DiscoveredModel]]) -> dict[str, list[str]]:
        """Save what each provider offers and is neither saved nor hidden (#93).

        Returns the aliases added per provider alias. A turned-off provider
        is left alone.
        """
        added: dict[str, list[str]] = {}
        for provider_alias, models in found.items():
            try:
                provider = self._providers.get(provider_alias)
            except NotFoundError:
                continue
            if not provider.enabled:
                continue
            hidden = set(self._hidden_ids(provider.id))
            saved = {model.provider_model_id for model in self._ctx.model_repo.list(provider.id)}
            for model in models:
                if model.provider_model_id in saved or model.provider_model_id in hidden:
                    continue
                alias = self._free_alias(provider.id, model.provider_model_id)
                record = self.add(
                    provider.id, model.provider_model_id, alias, capabilities=model.capabilities
                )
                record.availability = "available"
                self._ctx.model_repo.update(record)
                saved.add(model.provider_model_id)
                added.setdefault(provider.alias, []).append(alias)
        return added

    # -- lookup ---------------------------------------------------------

    def list(
        self, provider_ref: str | None = None, *, include_disabled: bool = True
    ) -> list[ModelRecord]:
        """Saved models. Without `include_disabled`, a turned-off provider's
        models are left out (plan 0.4.0 #97): that is what every picker sees."""
        if provider_ref is None:
            models = self._ctx.model_repo.list()
            if include_disabled:
                return models
            enabled = {p.id for p in self._providers.list(include_disabled=False)}
            return [m for m in models if m.provider_id in enabled]
        provider = self._providers.get(provider_ref)
        if not include_disabled and not provider.enabled:
            return []
        return self._ctx.model_repo.list(provider.id)

    def resolve(self, ref: str, provider_ref: str | None = None) -> ModelRecord:
        if provider_ref is not None:
            provider = self._providers.get(provider_ref)
            found = self._ctx.model_repo.get(ref)
            if found is not None and found.provider_id == provider.id:
                return found
            found = self._ctx.model_repo.get_by_alias(provider.id, ref)
            if found is not None:
                return found
            found = self._ctx.model_repo.get_by_provider_model_id(provider.id, ref)
            if found is not None:
                return found
            aliases = ", ".join(m.alias for m in self._ctx.model_repo.list(provider.id)) or "(none)"
            raise NotFoundError(
                f"Model not found for provider {provider.alias!r}: {ref}",
                hint=f"Known models: {aliases}",
            )

        by_id = self._ctx.model_repo.get(ref)
        if by_id is not None:
            return by_id

        by_alias = [m for m in self._ctx.model_repo.list() if m.alias == ref]
        if len(by_alias) == 1:
            return by_alias[0]
        by_model_id = [m for m in self._ctx.model_repo.list() if m.provider_model_id == ref]
        if len(by_alias) == 0 and len(by_model_id) == 1:
            return by_model_id[0]

        ambiguous = by_alias or by_model_id
        if ambiguous:
            providers = ", ".join(self._providers.get(m.provider_id).alias for m in ambiguous)
            raise InvalidUsageError(
                f"Model {ref!r} is ambiguous across providers: {providers}",
                hint="Pass the model ID explicitly or disambiguate with the provider.",
            )
        raise NotFoundError(f"Model not found: {ref}", hint="Use `rinari models list`.")

    def set_vision(self, ref: str, enabled: bool) -> ModelRecord:
        from dataclasses import replace

        if type(enabled) is not bool:
            raise ValueError("Vision must be boolean")
        record = self.resolve(ref)
        updated = replace(
            record,
            capabilities={**(record.capabilities or {}), "vision": enabled},
            updated_at=self._now(),
        )
        self._ctx.model_repo.update(updated)
        return updated

    def available(self, provider_ref: str | None = None) -> dict[str, list[DiscoveredModel]]:
        """Query provider discovery without saving anything."""
        provider = self._providers.get(provider_ref) if provider_ref else self._active_provider()
        if provider is None:
            raise NotFoundError("No provider selected", hint="Pass --provider <alias>.")
        self._providers.ensure_enabled(provider)
        adapter = adapter_for(provider, self._client)
        try:
            discovered = adapter.list_models(
                self._providers.resolve_secret(provider), provider.endpoint
            )
        except RinariError:
            raise
        from rinari.providers.metadata import model_metadata

        discovered = [
            replace(
                m,
                capabilities={
                    "source": "provider-discovery",
                    **model_metadata(provider, m.provider_model_id),
                    **(m.capabilities or {}),
                    "endpoint_capabilities": m.capabilities or {},
                },
            )
            for m in discovered
        ]
        data = {provider.alias: discovered}
        return data

    # -- lifecycle ------------------------------------------------------

    def add(
        self,
        provider_ref: str,
        provider_model_id: str,
        alias: str,
        capabilities: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> ModelRecord:
        provider = self._providers.get(provider_ref)
        transport = (settings or {}).get("transport")
        if transport is not None and transport not in ("chat", "responses", "anthropic"):
            raise InvalidUsageError(
                f"unknown transport {transport!r}",
                hint="Expected one of: chat, responses.",
            )
        existing = self._ctx.model_repo.get_by_provider_model_id(provider.id, provider_model_id)
        if existing is not None:
            raise ConflictError(
                f"Model {provider_model_id!r} is already saved as {existing.alias!r} "
                f"on {provider.alias!r}",
                hint=f"Use `rinari models alias {provider_model_id} {alias} "
                f"--provider {provider.alias}` to rename it.",
            )
        now = self._now()
        settings = dict(settings or {})
        if capabilities and capabilities.get("source"):
            settings["discovered_capabilities"] = capabilities.get(
                "endpoint_capabilities", capabilities
            )
            settings["discovered_at"] = now
            capabilities = None
        record = ModelRecord(
            id=self._ctx.ids.new("mdl"),
            alias=alias,
            provider_id=provider.id,
            provider_model_id=provider_model_id,
            settings=dict(settings or {}),
            capabilities=capabilities,
            availability="unknown",
            created_at=now,
            updated_at=now,
        )
        with self._ctx.db.transaction():
            self._ctx.model_repo.insert(record)
            if provider.default_model_id is None:
                provider.default_model_id = record.id
                provider.updated_at = now
                self._ctx.provider_repo.update(provider)
        return record

    def alias(self, ref: str, new_alias: str, provider_ref: str | None = None) -> ModelRecord:
        record = self.resolve(ref, provider_ref)
        if record.alias == new_alias:
            return record
        taken = self._ctx.model_repo.get_by_alias(record.provider_id, new_alias)
        if taken is not None:
            raise ConflictError(
                f"Model alias already in use: {new_alias}",
                hint="Pick another alias or remove the existing model first.",
            )
        with self._ctx.db.transaction():
            record.alias = new_alias
            record.updated_at = self._now()
            self._ctx.model_repo.update(record)
        return record

    def remove(self, ref: str, provider_ref: str | None = None) -> ModelRecord:
        record = self.resolve(ref, provider_ref)
        provider = self._ctx.provider_repo.get(record.provider_id)
        with self._ctx.db.transaction():
            self._ctx.model_repo.delete(record.id)
            # Hidden, not forgotten: a refresh does not bring it back (#93).
            hidden = self._hidden_ids(record.provider_id)
            if record.provider_model_id not in hidden:
                self._set_hidden(record.provider_id, [*hidden, record.provider_model_id])
            if provider is not None and provider.default_model_id == record.id:
                provider.default_model_id = None
                provider.updated_at = self._now()
                self._ctx.provider_repo.update(provider)
            active_model_id = self._ctx.config_repo.get(KEY_ACTIVE_MODEL)
            if active_model_id == record.id:
                self._ctx.config_repo.delete(KEY_ACTIVE_MODEL)
        return record

    # -- selection ------------------------------------------------------

    def use(self, ref: str, provider_ref: str | None = None) -> ResolvedModel:
        record = self.resolve(ref, provider_ref)
        provider = self._providers.get(record.provider_id)
        self._providers.ensure_enabled(provider, record.alias)
        active_id = self._ctx.config_repo.get(KEY_ACTIVE_PROVIDER)
        switched = active_id != provider.id
        now = self._now()
        with self._ctx.db.transaction():
            provider.default_model_id = record.id
            provider.last_used_model_id = record.id
            provider.updated_at = now
            self._ctx.provider_repo.update(provider)
            self._ctx.config_repo.set(
                ConfigValue(key=KEY_ACTIVE_PROVIDER, value=provider.id, updated_at=now)
            )
            self._ctx.config_repo.set(
                ConfigValue(key=KEY_ACTIVE_MODEL, value=record.id, updated_at=now)
            )
        return ResolvedModel(model=record, provider=provider, switched_provider=switched)

    def set_provider_default(self, provider_ref: str, model_ref: str) -> ModelRecord:
        model = self.resolve(model_ref, provider_ref)
        provider = self._providers.get(provider_ref)
        with self._ctx.db.transaction():
            provider.default_model_id = model.id
            provider.updated_at = self._now()
            self._ctx.provider_repo.update(provider)
        return model

    def reset(self, provider_ref: str | None = None) -> ResolvedModel | None:
        """Clear the session-level active model and fall back to the provider default."""
        provider = (
            self._providers.get(provider_ref)
            if provider_ref is not None
            else self._active_provider()
        )
        if provider is None:
            return None
        fallback = (
            self._ctx.model_repo.get(provider.default_model_id)
            or self._ctx.model_repo.get(provider.last_used_model_id)
            or next(iter(self._ctx.model_repo.list(provider.id)), None)
        )
        now = self._now()
        if fallback is None:
            self._ctx.config_repo.delete(KEY_ACTIVE_MODEL)
            return None
        with self._ctx.db.transaction():
            self._ctx.config_repo.set(
                ConfigValue(key=KEY_ACTIVE_MODEL, value=fallback.id, updated_at=now)
            )
        return ResolvedModel(model=fallback, provider=provider)

    def _active_provider(self) -> ProviderRecord | None:
        provider_id = self._ctx.config_repo.get(KEY_ACTIVE_PROVIDER)
        return self._ctx.provider_repo.get(provider_id) if provider_id else None

    # -- discovery / refresh / test --------------------------------------

    # A model the public catalog does not know yet: refresh it first, at most
    # this often (plan 0.4.0 #94: a Gemini model a day newer than the catalog
    # went out by the wrong protocol).
    CATALOG_REFRESH_EVERY_S = 3600
    _KEY_CATALOG_REFRESHED = "models.catalog.unknown_refresh_at"

    def _catalog_for_unknown(self, provider, discovered) -> None:
        from rinari.providers import metadata
        from rinari.providers.catalog import product_for

        product = product_for(provider)
        if product not in metadata.PUBLIC_PRODUCTS:
            return
        known = metadata.catalog()["models"].get(product, {})
        if all(model_id in known for model_id in discovered):
            return
        now = self._ctx.clock.now()
        last = self._ctx.config_repo.get(self._KEY_CATALOG_REFRESHED)
        try:
            if last is not None and now - float(last) < self.CATALOG_REFRESH_EVERY_S:
                return
        except ValueError:
            pass
        from rinari.storage.records import ConfigValue

        self._ctx.config_repo.set(
            ConfigValue(key=self._KEY_CATALOG_REFRESHED, value=str(now), updated_at=self._now())
        )
        try:
            get = self._client.get if self._client is not None else httpx.get
            self.last_catalog_refresh = metadata.refresh_public_catalog(get)
        except Exception as exc:  # offline: the catalog already in use stays
            self.last_catalog_refresh = {"error": f"{exc.__class__.__name__}: {exc}"}

    def refresh(
        self,
        provider_ref: str | None = None,
        *,
        add_new: bool = False,
        public_catalog: bool = False,
    ) -> dict[str, RefreshResult]:
        """Re-read what each provider offers and update the saved models.

        With ``add_new`` a model the provider now offers and is not saved yet
        is saved under its provider ID, and providers without saved models are
        read too. With ``public_catalog`` the public model list (context
        windows, modalities) is downloaded first: a provider's own model list
        often names models without saying how large they are.
        """
        if public_catalog:
            from rinari.providers import metadata

            try:
                get = self._client.get if self._client is not None else httpx.get
                self.last_catalog_refresh = metadata.refresh_public_catalog(get)
            except Exception as exc:  # offline: keep the catalog already in use
                self.last_catalog_refresh = {
                    "error": f"{exc.__class__.__name__}: {getattr(exc, 'message', exc)}"
                }
        provider = self._providers.get(provider_ref) if provider_ref else None
        providers = [provider] if provider else self._providers.list()
        results: dict[str, RefreshResult] = {}
        for rec in providers:
            models = self._ctx.model_repo.list(rec.id)
            if not rec.enabled:
                # Turned off (#97): nothing reads it, its saved models stay as they are.
                results[rec.alias] = RefreshResult(
                    saved=len(models),
                    still_available=0,
                    marked_unavailable=0,
                    discovered=0,
                    error="disabled",
                )
                continue
            if not models and not add_new:
                results[rec.alias] = RefreshResult(0, 0, 0, 0)
                continue
            adapter = adapter_for(rec, self._client)
            try:
                discovered = {
                    m.provider_model_id: m.capabilities
                    for m in adapter.list_models(self._providers.resolve_secret(rec), rec.endpoint)
                }
            except Exception as exc:
                results[rec.alias] = RefreshResult(
                    saved=len(models),
                    still_available=0,
                    marked_unavailable=0,
                    discovered=0,
                    error=f"{exc.__class__.__name__}: {getattr(exc, 'message', exc)}",
                )
                continue
            if add_new and not public_catalog:
                self._catalog_for_unknown(rec, discovered)
            from rinari.context import windows

            # The context resolver shares this discovery instead of repeating it,
            # and an explicit refresh never waits for its cache to expire.
            windows.remember(self._ctx, rec, discovered)
            added: list[str] = []
            if add_new:
                saved = {model.provider_model_id for model in models}
                hidden = set(self._hidden_ids(rec.id))
                for provider_model_id in discovered:
                    if provider_model_id in saved or provider_model_id in hidden:
                        continue
                    alias = self._free_alias(rec.id, provider_model_id)
                    models.append(self.add(rec.id, provider_model_id, alias))
                    added.append(alias)
            now = self._now()
            still = 0
            gone = 0
            with self._ctx.db.transaction():
                for model in models:
                    available = model.provider_model_id in discovered
                    model.availability = "available" if available else "unavailable"
                    if available:
                        model.settings = {
                            **(model.settings or {}),
                            "discovered_capabilities": {
                                **(discovered[model.provider_model_id] or {}),
                            },
                            "discovered_at": now,
                        }
                    model.updated_at = now
                    self._ctx.model_repo.update(model)
                    if available:
                        still += 1
                    else:
                        gone += 1
            results[rec.alias] = RefreshResult(
                saved=len(models),
                still_available=still,
                marked_unavailable=gone,
                discovered=len(discovered),
                added=tuple(added),
            )
        return results

    def _free_alias(self, provider_id: str, wanted: str) -> str:
        alias, n = wanted, 2
        while self._ctx.model_repo.get_by_alias(provider_id, alias) is not None:
            alias, n = f"{wanted}-{n}", n + 1
        return alias

    def test(self, ref: str, provider_ref: str | None = None) -> ModelTestResult:
        """A real minimal call by the model's protocol (plan 0.4.0 #94): «works ·
        google protocol · 0.7 s», or why not. Being in the provider's list said
        nothing about whether the model could be used."""
        import time

        from rinari.models.router import ModelRouter, _resolve_transport
        from rinari.models.types import ChatMessage, ModelRequest

        record = self.resolve(ref, provider_ref)
        provider = self._providers.get(record.provider_id)
        router = ModelRouter(self._providers, self, http_client=self._client)
        started = time.monotonic()
        try:
            router.invoke(
                provider,
                record.id,
                ModelRequest(
                    model=record.provider_model_id,
                    messages=(ChatMessage.user("Reply with OK."),),
                    max_tokens=32,
                ),
            )
        except Exception as exc:
            return ModelTestResult(
                ok=False,
                detail=str(getattr(exc, "message", exc))[:300],
                model=record,
                provider=provider,
            )
        seconds = time.monotonic() - started
        current = self.resolve(record.id)
        protocol = _resolve_transport(provider, current)
        return ModelTestResult(
            ok=True,
            detail=f"works · {protocol} protocol · {seconds:.1f} s",
            model=current,
            provider=provider,
        )
