"""Model Registry Service (docs/commands.md sections 17-20, 72).

Models are saved per provider and resolved by alias, provider model ID,
or internal ID. `use` persists the selection in config_values and updates
the provider's default/last-used model (provider-specific model memory).
Refresh marks temporarily missing models unavailable instead of deleting
them.
"""

from __future__ import annotations

from dataclasses import dataclass
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

    def _now(self) -> str:
        return now_iso(self._ctx.clock)

    # -- lookup ---------------------------------------------------------

    def list(self, provider_ref: str | None = None) -> list[ModelRecord]:
        if provider_ref is None:
            return self._ctx.model_repo.list()
        provider = self._providers.get(provider_ref)
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
        updated = replace(record, capabilities={**(record.capabilities or {}), "vision": enabled},
                          updated_at=self._now())
        self._ctx.model_repo.update(updated)
        return updated

    def available(self, provider_ref: str | None = None) -> dict[str, list[DiscoveredModel]]:
        """Query provider discovery without saving anything."""
        provider = self._providers.get(provider_ref) if provider_ref else self._active_provider()
        if provider is None:
            raise NotFoundError("No provider selected", hint="Pass --provider <alias>.")
        adapter = adapter_for(provider, self._client)
        try:
            discovered = adapter.list_models(
                self._providers.resolve_secret(provider), provider.endpoint
            )
        except RinariError:
            raise
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
        if transport is not None and transport not in ("chat", "responses"):
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

    def refresh(self, provider_ref: str | None = None) -> dict[str, RefreshResult]:
        provider = self._providers.get(provider_ref) if provider_ref else None
        providers = [provider] if provider else self._providers.list()
        results: dict[str, RefreshResult] = {}
        for rec in providers:
            models = self._ctx.model_repo.list(rec.id)
            if not models:
                results[rec.alias] = RefreshResult(0, 0, 0, 0)
                continue
            adapter = adapter_for(rec, self._client)
            try:
                discovered = {
                    m.provider_model_id
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
            now = self._now()
            still = 0
            gone = 0
            with self._ctx.db.transaction():
                for model in models:
                    available = model.provider_model_id in discovered
                    model.availability = "available" if available else "unavailable"
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
            )
        return results

    def test(self, ref: str, provider_ref: str | None = None) -> ModelTestResult:
        record = self.resolve(ref, provider_ref)
        provider = self._providers.get(record.provider_id)
        adapter = adapter_for(provider, self._client)
        health = adapter.health(self._providers.resolve_secret(provider), provider.endpoint)
        if not health.connected:
            return ModelTestResult(
                ok=False,
                detail=health.detail or "provider not reachable",
                model=record,
                provider=provider,
            )
        found = any(m.provider_model_id == record.provider_model_id for m in health.models)
        return ModelTestResult(
            ok=found,
            detail=(
                f"model {record.provider_model_id!r} present in provider catalog"
                if found
                else f"model {record.provider_model_id!r} not found in provider catalog"
            ),
            model=record,
            provider=provider,
        )
