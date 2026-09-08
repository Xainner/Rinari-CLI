"""Provider Registry Service (docs/commands.md sections 13-15, 71).

Owns the active selection (config_values), credential references, and
adapter-backed health checks. Architectural invariant: switching the
active provider (`use`) never deletes providers, models, or credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from rinari.application.context import AppContext
from rinari.application.credentials import CredentialStore
from rinari.providers.adapters.base import ProviderHealth
from rinari.providers.registry import adapter_for, validate_provider_type
from rinari.shared.clock import now_iso
from rinari.shared.errors import ConflictError, InvalidUsageError, NotFoundError
from rinari.storage.records import (
    ConfigValue,
    ProviderCredentialRef,
    ProviderRecord,
)


@dataclass(frozen=True, slots=True)
class AddProviderInput:
    alias: str
    provider_type: str
    auth_method: str = "api-key"
    endpoint: str | None = None
    account_hint: str | None = None
    secret: str | None = None
    secret_env: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    provider: ProviderRecord
    model: Any | None  # ModelRecord | None (kept Any to avoid a hard import cycle)


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    source: str
    name: str
    detail: str
    provider_type: str
    endpoint: str | None = None


KEY_ACTIVE_PROVIDER = "active_provider"
KEY_ACTIVE_MODEL = "active_model"


class ProviderService:
    def __init__(
        self,
        ctx: AppContext,
        http_client: httpx.Client | None = None,
        credentials: CredentialStore | None = None,
    ) -> None:
        self._ctx = ctx
        self._client = http_client
        self._credentials = credentials or CredentialStore(ctx.layout)

    def _now(self) -> str:
        return now_iso(self._ctx.clock)

    # -- lookup ---------------------------------------------------------

    def list(self) -> list[ProviderRecord]:
        return self._ctx.provider_repo.list()

    def get(self, ref: str) -> ProviderRecord:
        record = self._ctx.provider_repo.get(ref)
        if record is None:
            record = self._ctx.provider_repo.get_by_alias(ref)
        if record is None:
            known = [p.alias for p in self.list()]
            hint = f"Known providers: {', '.join(known)}." if known else "No providers saved yet."
            raise NotFoundError(f"Provider not found: {ref}", hint=hint)
        return record

    # -- lifecycle ------------------------------------------------------

    def add(self, input: AddProviderInput) -> ProviderRecord:
        validate_provider_type(
            input.provider_type, input.auth_method, input.settings.get("protocol")
        )
        if self._ctx.provider_repo.get_by_alias(input.alias) is not None:
            raise ConflictError(
                f"Provider alias already exists: {input.alias}",
                hint="Use another alias or `rinari providers rename`.",
            )
        if input.secret is None and input.secret_env is None and input.auth_method != "none":
            raise InvalidUsageError(
                "A credential is required",
                hint="Pass --api-key <value> or --api-key-env <VAR>, or use --no-auth.",
            )
        if input.secret is not None and input.secret_env is not None:
            raise InvalidUsageError("Use either --api-key or --api-key-env, not both")

        now = self._now()
        record = ProviderRecord(
            id=self._ctx.ids.new("prov"),
            alias=input.alias,
            type=input.provider_type,
            auth_method=input.auth_method,
            account_hint=input.account_hint,
            endpoint=input.endpoint,
            settings=dict(input.settings),
            default_model_id=None,
            last_used_model_id=None,
            status_connected=None,
            status_checked_at=None,
            created_at=now,
            updated_at=now,
        )
        with self._ctx.db.transaction():
            self._ctx.provider_repo.insert(record)
            if record.auth_method != "none":
                if input.secret is not None:
                    secret_ref = self._credentials.store_provider_secret(record.id, input.secret)
                else:
                    secret_ref = f"env://{input.secret_env}"
                self._ctx.provider_repo.set_credential(
                    ProviderCredentialRef(
                        provider_id=record.id,
                        secret_ref=secret_ref,
                        method=record.auth_method,
                        updated_at=now,
                    )
                )
            if self._ctx.config_repo.get(KEY_ACTIVE_PROVIDER) is None:
                self._ctx.config_repo.set(
                    ConfigValue(key=KEY_ACTIVE_PROVIDER, value=record.id, updated_at=now)
                )
        return record

    def rename(self, ref: str, new_alias: str) -> ProviderRecord:
        record = self.get(ref)
        if new_alias == record.alias:
            return record
        if self._ctx.provider_repo.get_by_alias(new_alias) is not None:
            raise ConflictError(f"Provider alias already exists: {new_alias}")
        with self._ctx.db.transaction():
            record.alias = new_alias
            record.updated_at = self._now()
            self._ctx.provider_repo.update(record)
        return record

    def update(
        self,
        ref: str,
        *,
        endpoint: str | None = None,
        settings: dict[str, Any] | None = None,
        account_hint: str | None = None,
    ) -> ProviderRecord:
        """Edit connection fields in place (endpoint/settings/hint).

        Renames and credential rotation stay in `rename`/`set_auth`.
        Changing the endpoint invalidates the last health check.
        """
        record = self.get(ref)
        changed = False
        if endpoint is not None:
            if not endpoint.strip():
                raise InvalidUsageError("Endpoint must be a non-empty string")
            record.endpoint = endpoint.strip()
            record.status_connected = None
            record.status_checked_at = None
            changed = True
        if settings is not None:
            if not isinstance(settings, dict):
                raise InvalidUsageError("Settings must be an object")
            record.settings = dict(settings)
            changed = True
        if account_hint is not None:
            if not isinstance(account_hint, str):
                raise InvalidUsageError("Account hint must be a string")
            record.account_hint = account_hint
            changed = True
        if changed:
            record.updated_at = self._now()
            self._ctx.provider_repo.update(record)
        return record

    def remove(
        self, ref: str, switch_to: str | None = None, keep_credentials: bool = False
    ) -> ProviderRecord:
        record = self.get(ref)
        active_id = self._ctx.config_repo.get(KEY_ACTIVE_PROVIDER)
        was_active = active_id == record.id
        if was_active and switch_to is None:
            raise InvalidUsageError(
                f"Provider {record.alias!r} is active. Select another provider first.",
                hint=f"rinari providers remove {record.alias} --switch-to <alias>",
            )
        target = self.get(switch_to) if switch_to is not None else None

        credential = self._ctx.provider_repo.get_credential(record.id)
        with self._ctx.db.transaction():
            self._ctx.model_repo.delete_by_provider(record.id)
            if was_active:
                now = self._now()
                if target is not None:
                    fallback = self._default_model_for(target)
                    self._ctx.config_repo.set(
                        ConfigValue(key=KEY_ACTIVE_PROVIDER, value=target.id, updated_at=now)
                    )
                    if fallback is not None:
                        self._ctx.config_repo.set(
                            ConfigValue(key=KEY_ACTIVE_MODEL, value=fallback.id, updated_at=now)
                        )
                    else:
                        self._ctx.config_repo.delete(KEY_ACTIVE_MODEL)
                else:
                    self._ctx.config_repo.delete(KEY_ACTIVE_PROVIDER)
                    self._ctx.config_repo.delete(KEY_ACTIVE_MODEL)
            self._ctx.provider_repo.delete(record.id)
        if credential is not None and not keep_credentials:
            self._credentials.delete(credential.secret_ref)
        return record

    def set_auth(
        self, ref: str, secret: str | None = None, secret_env: str | None = None
    ) -> ProviderRecord:
        record = self.get(ref)
        if (secret is None) == (secret_env is None):
            raise InvalidUsageError("Pass exactly one of --api-key or --api-key-env")
        now = self._now()
        with self._ctx.db.transaction():
            existing = self._ctx.provider_repo.get_credential(record.id)
            if existing is not None:
                self._credentials.delete(existing.secret_ref)
            if secret is not None:
                secret_ref = self._credentials.store_provider_secret(record.id, secret)
            else:
                secret_ref = f"env://{secret_env}"
            self._ctx.provider_repo.set_credential(
                ProviderCredentialRef(
                    provider_id=record.id,
                    secret_ref=secret_ref,
                    method="api-key",
                    updated_at=now,
                )
            )
            record.auth_method = "api-key"
            record.status_connected = None
            record.status_checked_at = None
            record.updated_at = now
            self._ctx.provider_repo.update(record)
        return record

    def logout(self, ref: str) -> ProviderRecord:
        record = self.get(ref)
        credential = self._ctx.provider_repo.get_credential(record.id)
        with self._ctx.db.transaction():
            if credential is not None:
                self._credentials.delete(credential.secret_ref)
                self._ctx.provider_repo.delete_credential(record.id)
            record.status_connected = False
            record.updated_at = self._now()
            self._ctx.provider_repo.update(record)
        return record

    def login(self, ref: str) -> ProviderRecord:
        record = self.get(ref)
        try:
            result = adapter_for(record, self._client).login()
        except NotImplementedError:
            raise InvalidUsageError(
                f"Provider type {record.type!r} does not support interactive login yet",
                hint=(
                    f"Use `rinari providers auth {record.alias} --api-key <value>` or "
                    f"--api-key-env <VAR> instead."
                ),
            ) from None
        if not result.connected:
            raise InvalidUsageError(
                f"Login failed for {record.alias!r}: {result.detail or 'see provider'}"
            )
        with self._ctx.db.transaction():
            record.status_connected = True
            record.status_checked_at = self._now()
            self._ctx.provider_repo.update(record)
        return record

    # -- active selection ------------------------------------------------

    def use(self, ref: str) -> ProviderSelection:
        record = self.get(ref)
        model = self._default_model_for(record)
        now = self._now()
        with self._ctx.db.transaction():
            self._ctx.config_repo.set(
                ConfigValue(key=KEY_ACTIVE_PROVIDER, value=record.id, updated_at=now)
            )
            if model is not None:
                self._ctx.config_repo.set(
                    ConfigValue(key=KEY_ACTIVE_MODEL, value=model.id, updated_at=now)
                )
            else:
                self._ctx.config_repo.delete(KEY_ACTIVE_MODEL)
        return ProviderSelection(provider=record, model=model)

    def current(self) -> ProviderSelection | None:
        provider_id = self._ctx.config_repo.get(KEY_ACTIVE_PROVIDER)
        if provider_id is None:
            return None
        provider = self._ctx.provider_repo.get(provider_id)
        if provider is None:
            return None
        model_id = self._ctx.config_repo.get(KEY_ACTIVE_MODEL)
        model = self._ctx.model_repo.get(model_id) if model_id else None
        if model is not None and model.provider_id != provider.id:
            model = None
        if model is None:
            # Resolution order (commands.md #20): explicit active model, then the
            # provider's own default / last-used model. `models add` sets the
            # provider default, so a fresh provider+model pair is immediately usable.
            model = self._default_model_for(provider)
        return ProviderSelection(provider=provider, model=model)

    # -- adapter-backed operations ---------------------------------------

    def model_alias(self, model_id: str | None) -> str | None:
        if model_id is None:
            return None
        model = self._ctx.model_repo.get(model_id)
        return model.alias if model is not None else None

    def _default_model_for(self, record: ProviderRecord):
        models = self._ctx.model_repo.list(record.id)
        if not models:
            return None
        for model_id in (record.default_model_id, record.last_used_model_id):
            if model_id:
                candidate = self._ctx.model_repo.get(model_id)
                if candidate is not None and candidate.provider_id == record.id:
                    return candidate
        return models[0]

    def credential_ref(self, record: ProviderRecord) -> str | None:
        credential = self._ctx.provider_repo.get_credential(record.id)
        return credential.secret_ref if credential else None

    def resolve_secret(self, record: ProviderRecord) -> str | None:
        ref = self.credential_ref(record)
        if ref is None:
            return None
        return self._credentials.resolve(ref)

    def test(self, ref: str) -> ProviderHealth:
        record = self.get(ref)
        adapter = adapter_for(record, self._client)
        health = adapter.health(self.resolve_secret(record), record.endpoint)
        with self._ctx.db.transaction():
            record.status_connected = health.connected
            record.status_checked_at = self._now()
            self._ctx.provider_repo.update(record)
        return health

    def discover(self) -> list[DiscoveryCandidate]:
        candidates: list[DiscoveryCandidate] = []
        for name, url, probe in (
            ("ollama", "http://127.0.0.1:11434", "/api/tags"),
            ("lmstudio", "http://127.0.0.1:1234", "/v1/models"),
        ):
            try:
                response = self._http().get(f"{url}{probe}", timeout=2.0)
                if response.status_code == 200:
                    candidates.append(
                        DiscoveryCandidate(
                            source="local-endpoint",
                            name=name,
                            detail=f"reachable at {url}",
                            provider_type="custom",
                            endpoint=f"{url}/v1",
                        )
                    )
            except httpx.HTTPError:
                continue
        for var, provider_type in (
            ("OPENAI_API_KEY", "openai"),
            ("ANTHROPIC_API_KEY", "anthropic"),
        ):
            if os.environ.get(var):
                candidates.append(
                    DiscoveryCandidate(
                        source="environment",
                        name=var,
                        detail="credential reference available in the environment",
                        provider_type=provider_type,
                    )
                )
        return candidates

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=10.0)
        return self._client
