"""Provider Registry Service (docs/commands.md sections 13-15, 71).

Owns the active selection (config_values), credential references, and
adapter-backed health checks. Architectural invariant: switching the
active provider (`use`) never deletes providers, models, or credentials.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from rinari.application.context import AppContext
from rinari.application.credentials import (
    CredentialStore,
    validate_env_name,
)
from rinari.providers.adapters.base import ProviderHealth
from rinari.providers.registry import adapter_for, validate_provider_type
from rinari.shared.clock import now_iso
from rinari.shared.errors import ConflictError, InvalidUsageError, NotFoundError
from rinari.shared.locking import file_lock
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

    def _credential_lock(self) -> Any:
        """Exclusión con la limpieza del vault: un alta escribe la credencial
        dentro de su transacción y hasta el commit otra conexión no ve la fila.
        """
        lock_path = getattr(self._credentials, "lock_path", None)
        if lock_path is None:
            return contextlib.nullcontext()
        return file_lock(lock_path)

    def add(self, input: AddProviderInput) -> ProviderRecord:
        with self._credential_lock():
            return self._add(input)

    def _add(self, input: AddProviderInput) -> ProviderRecord:
        validate_provider_type(
            input.provider_type, input.auth_method, input.settings.get("protocol")
        )
        if self._ctx.provider_repo.get_by_alias(input.alias) is not None:
            raise ConflictError(
                f"Provider alias already exists: {input.alias}",
                hint="Use another alias or `rinari providers rename`.",
            )
        if (
            input.secret is None
            and input.secret_env is None
            and input.auth_method not in ("none", "oauth")
        ):
            raise InvalidUsageError(
                "A credential is required",
                hint="Pass --api-key <value> or --api-key-env <VAR>, or use --no-auth.",
            )
        if input.secret is not None and input.secret_env is not None:
            raise InvalidUsageError("Use either --api-key or --api-key-env, not both")
        # F1: validate the env NAME before any write. A rejected reference is
        # never persisted and never echoed back (it may hold a pasted key).
        if input.secret_env is not None:
            validate_env_name(input.secret_env)
        # F5: a provider without any usable base URL would persist fine and
        # then fail on every call; reject it up front.
        if input.provider_type == "custom" and not (input.endpoint or "").strip():
            raise InvalidUsageError(
                "A custom provider requires an endpoint",
                hint="Pass --endpoint https://host/v1 (or a base_url setting).",
            )

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
            if record.auth_method == "oauth":
                from rinari.providers.catalog import product_for

                if (
                    product_for(record) not in ("chatgpt", "github-copilot")
                    or input.secret
                    or input.secret_env
                ):
                    raise InvalidUsageError(
                        "Use interactive login with a supported subscription endpoint."
                    )
            if record.auth_method not in ("none", "oauth"):
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
            # F5: the same registry rules as creation apply on edit; an
            # unknown adapter protocol must not be persisted via update.
            validate_provider_type(
                record.type,
                record.auth_method,
                settings.get("protocol", record.settings.get("protocol")),
            )
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

    def apply_update(
        self,
        ref: str,
        *,
        new_alias: str | None = None,
        endpoint: str | None = None,
        settings: dict[str, Any] | None = None,
        account_hint: str | None = None,
        secret: str | None = None,
        secret_env: str | None = None,
    ) -> ProviderRecord:
        """Atomic combined edit (F4/F5): validate everything, then apply.

        Absent fields mean "leave unchanged"; a rename, a credential source
        change and connection fields are validated as one operation so a
        rejected update leaves no partial changes (alias, credential or
        settings already mutated) behind. Credential storage lives outside
        SQL, so the credential path reuses the rotation-safe `set_auth`
        ordering: write the new value first, retire the old one only after
        the record update commits.
        """
        record = self.get(ref)
        if new_alias is not None:
            new_alias = new_alias.strip()
            if not new_alias:
                raise InvalidUsageError("Alias must be a non-empty string")
        if secret is not None and secret_env is not None:
            raise InvalidUsageError("Use either --api-key or --api-key-env, not both")
        if secret_env is not None:
            # F1/F2: a malformed env reference must be rejected before any
            # state (or secret) changes, keeping the previous credential.
            validate_env_name(secret_env)
        if (
            secret is None
            and secret_env is None
            and new_alias is None
            and endpoint is None
            and settings is None
            and account_hint is None
        ):
            raise InvalidUsageError(
                "Nothing to update: pass alias, endpoint, settings, account_hint, "
                "secret or secret_env."
            )

        # -- validation phase (no writes) --------------------------------
        if settings is not None:
            if not isinstance(settings, dict):
                raise InvalidUsageError("Settings must be an object")
            validate_provider_type(
                record.type,
                record.auth_method,
                settings.get("protocol", record.settings.get("protocol")),
            )
        if (
            new_alias is not None
            and new_alias != record.alias
            and self._ctx.provider_repo.get_by_alias(new_alias) is not None
        ):
            raise ConflictError(f"Provider alias already exists: {new_alias}")
        if endpoint is not None and not endpoint.strip():
            raise InvalidUsageError("Endpoint must be a non-empty string")

        # -- apply phase --------------------------------------------------
        now = self._now()
        previous_ref: str | None = None
        staged_ref: str | None = None
        with self._credential_lock():
            existing = self._ctx.provider_repo.get_credential(record.id)
            previous_ref = existing.secret_ref if existing is not None else None
            try:
                # Rotation protocol (review P1): stage the new value under a
                # FRESH reference *before* any destructive step. The previous
                # resolvable value is never overwritten, so neither an SQL
                # failure, a failed cleanup nor a process kill can lose the
                # currently confirmed credential.
                if secret is not None:
                    staged_ref = self._stage_candidate(record.id, secret)
                with self._ctx.db.transaction():
                    if staged_ref is not None or secret_env is not None:
                        secret_ref = staged_ref if staged_ref is not None else f"env://{secret_env}"
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
                    if new_alias is not None:
                        record.alias = new_alias
                    if endpoint is not None:
                        record.endpoint = endpoint.strip()
                        record.status_connected = None
                        record.status_checked_at = None
                    if settings is not None:
                        record.settings = dict(settings)
                    if account_hint is not None:
                        record.account_hint = account_hint
                    record.updated_at = now
                    if staged_ref is not None:
                        self._ctx.provider_repo.remove_pending_credential_cleanup(staged_ref)
                    self._ctx.provider_repo.update(record)
                    # Pending row inside the same transaction: a crash before
                    # or after the commit leaves the old reference tracked.
                    if previous_ref is not None and previous_ref != (
                        staged_ref or f"env://{secret_env}"
                    ):
                        self._ctx.provider_repo.add_pending_credential_cleanup(
                            record.id, previous_ref, now
                        )
            except Exception:
                # SQL rolled back (or staging failed): the confirmed record
                # still points at the previous reference, which was never
                # modified. Only the fresh candidate needs cleanup, and its
                # failure is recorded, never fatal.
                if staged_ref is not None:
                    with contextlib.suppress(Exception):
                        self._retire_previous_reference(record.id, staged_ref, "", now)
                raise
        # Retire the previous secret only after the new state committed and
        # only if the reference actually changed (rotation-safe ordering). A
        # crash from here on leaves the previous copy as a resolvable orphan
        # until `rinari secrets cleanup` removes it — never a lost key. A
        # FAILED delete never propagates: the update is already confirmed;
        # the old reference is recorded as pending cleanup instead.
        if secret is not None or secret_env is not None:
            current = self._ctx.provider_repo.get_credential(record.id)
            if previous_ref is not None and current is not None:
                self._retire_previous_reference(record.id, previous_ref, current.secret_ref, now)
        return record

    def remove(
        self, ref: str, switch_to: str | None = None, keep_credentials: bool = False
    ) -> ProviderRecord:
        with self._credential_lock():
            return self._remove(ref, switch_to=switch_to, keep_credentials=keep_credentials)

    def _remove(
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
            if keep_credentials and credential is not None:
                # La retención se registra en su propia tabla: la metadata se
                # borra en cascada con el proveedor y no sirve para esto.
                self._ctx.provider_repo.retain_credential(credential, retained_at=self._now())
            self._ctx.provider_repo.delete(record.id)
        if credential is not None and not keep_credentials:
            self._credentials.delete(credential.secret_ref)
        return record

    def _stage_candidate(self, provider_id: str, secret: str) -> str:
        def record_candidate(ref):
            with self._ctx.db.transaction():
                self._ctx.provider_repo.add_pending_credential_cleanup(
                    provider_id, ref, self._now()
                )

        return self._credentials.stage_unique_provider_secret(
            provider_id, secret, before_write=record_candidate
        )

    def _retire_previous_reference(
        self,
        provider_id: str,
        previous_ref: str,
        current_ref: str,
        now: str,
    ) -> bool:
        """Post-commit attempt to delete the replaced credential reference.

        Review rework: the pending row is registered INSIDE the rotation
        transaction (see _set_auth_locked/apply_update), so a crash between
        commit and this call still leaves the old reference tracked. This
        method only tries to complete the work: when the store delete
        succeeds — or the value is already gone — the pending row is
        removed. Any other failure keeps the row for a later attempt and
        never raises: the update is confirmed, cleanup failure must not
        report the whole operation as failed.
        """
        if previous_ref == current_ref:
            return True
        try:
            self._credentials.delete(previous_ref)
            if self._credentials.exists(previous_ref):
                return False
            with self._ctx.db.transaction():
                self._ctx.provider_repo.remove_pending_credential_cleanup(previous_ref)
        except Exception:
            return False  # pending row stays; a later run retries it
        return True

    def run_pending_credential_cleanup(self, *, lock_timeout: float = 5.0) -> int:
        """Sweep pending rows: delete the store value, retire the row.

        Idempotent: a missing store value completes the cleanup (the delete
        already happened); a store ACCESS error keeps the row for retry.
        Runs best-effort after rotations and is exposed via
        `rinari secrets cleanup`; never raises for individual entries.
        """
        completed = 0
        with file_lock(self._ctx.layout.credentials_lock, timeout=lock_timeout):
            protected = {
                row["secret_ref"]
                for row in self._ctx.db.query(
                    "SELECT secret_ref FROM provider_credentials_metadata "
                    "UNION SELECT secret_ref FROM retained_credentials"
                )
            }
            for entry in self._ctx.provider_repo.list_pending_credential_cleanup():
                secret_ref = entry["secret_ref"]
                if secret_ref in protected:
                    continue
                if self._retire_previous_reference(
                    entry["provider_id"], secret_ref, "", self._now()
                ):
                    completed += 1
        return completed

    def set_auth(
        self, ref: str, secret: str | None = None, secret_env: str | None = None
    ) -> ProviderRecord:
        record = self.get(ref)
        if (secret is None) == (secret_env is None):
            raise InvalidUsageError("Pass exactly one of --api-key or --api-key-env")
        # F2: validate the new source structurally *before* touching state.
        # A malformed env reference must not retire the previous credential.
        if secret_env is not None:
            validate_env_name(secret_env)
        with self._credential_lock():
            return self._set_auth_locked(record, secret, secret_env, self._now())

    def _set_auth_locked(
        self,
        record: ProviderRecord,
        secret: str | None,
        secret_env: str | None,
        now: str,
        auth_method: str = "api-key",
    ) -> ProviderRecord:
        existing = self._ctx.provider_repo.get_credential(record.id)
        previous_ref = existing.secret_ref if existing is not None else None
        staged_ref: str | None = None
        try:
            # Rotation protocol (review P1): stage the new value under a
            # FRESH reference *before* any destructive step; see apply_update.
            if secret is not None:
                staged_ref = self._stage_candidate(record.id, secret)
            with self._ctx.db.transaction():
                if staged_ref is not None or secret_env is not None:
                    secret_ref = staged_ref if staged_ref is not None else f"env://{secret_env}"
                    self._ctx.provider_repo.set_credential(
                        ProviderCredentialRef(
                            provider_id=record.id,
                            secret_ref=secret_ref,
                            method=auth_method,
                            updated_at=now,
                        )
                    )
                    record.auth_method = auth_method
                    record.status_connected = None
                    record.status_checked_at = None
                    record.updated_at = now
                    if staged_ref is not None:
                        self._ctx.provider_repo.remove_pending_credential_cleanup(staged_ref)
                    self._ctx.provider_repo.update(record)
                # The pending row is part of the same transaction: a crash
                # before or after the commit leaves the old reference tracked
                # exactly once (review: coordination, not suppression).
                if previous_ref is not None and previous_ref != (
                    staged_ref or f"env://{secret_env}"
                ):
                    self._ctx.provider_repo.add_pending_credential_cleanup(
                        record.id, previous_ref, now
                    )
        except Exception:
            # SQL rolled back (or staging failed): the confirmed record still
            # points at the untouched previous reference. Only the candidate
            # needs cleanup; its failure is never fatal.
            if staged_ref is not None:
                with contextlib.suppress(Exception):
                    self._retire_previous_reference(record.id, staged_ref, "", now)
            raise
        # Retire the previous reference only after the new state committed.
        # Never raises (review P1): a failed delete records the old reference
        # as pending cleanup instead of failing a confirmed rotation.
        if previous_ref is not None:
            current = self._ctx.provider_repo.get_credential(record.id)
            if current is not None:
                self._retire_previous_reference(record.id, previous_ref, current.secret_ref, now)
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
        if record.auth_method == "oauth":
            from rinari.providers.auth import token_for

            return token_for(self, record)
        ref = self.credential_ref(record)
        if ref is None:
            return None
        return self._credentials.resolve(ref)

    def set_oauth(self, ref, bundle):
        import json

        record = self.get(ref)
        if record.auth_method != "oauth":
            raise InvalidUsageError("Provider is not configured for subscription login.")
        with self._credential_lock():
            return self._set_auth_locked(
                record, json.dumps(bundle), None, self._now(), auth_method="oauth"
            )

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
