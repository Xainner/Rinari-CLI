"""Credential store: secret references and backends.

Records hold *references* (`env://VAR`, `file://providers/<id>`), never
values. Values are resolved at use time through a backend. Plain config
files and the database must never contain secret plaintext
(docs/commands.md section 24).
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rinari.shared.errors import (
    AuthenticationRequiredError,
    CredentialWriteError,
    InvalidUsageError,
)
from rinari.shared.locking import file_lock
from rinari.shared.paths import HomeLayout, home_identifier

ENV_SCHEME = "env://"
FILE_SCHEME = "file://"
KEYRING_SCHEME = "keyring://"

#: Opt-out for the OS credential store (tests force hermetic file storage).
KEYRING_ENV_DISABLE = "RINARI_KEYRING"

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_env_name(name: str) -> str:
    """Validate an environment-variable name, raising a redacted error.

    The rejected text is never interpolated into the message: an env-ref
    field commonly receives a pasted API key by mistake, and echoing it
    would leak the secret into errors, logs and protocol events
    (providers/PLAN_DE_TRABAJO.md F1).
    """
    if not name:
        raise InvalidUsageError(
            "Invalid environment secret reference: the variable name is empty",
            hint="Pass a variable name such as OPENAI_API_KEY, not the key value.",
        )
    if not _ENV_NAME_RE.match(name):
        raise InvalidUsageError(
            "Invalid environment secret reference: the variable name may only "
            "contain letters, digits and underscores, and must not start with "
            "a digit",
            hint=(
                "Pass a variable NAME such as OPENAI_API_KEY. If you pasted an "
                "API key, put it in the API key field instead."
            ),
        )
    return name


@dataclass(frozen=True, slots=True)
class SecretRef:
    scheme: str
    key: str


def parse_secret_ref(ref: str) -> SecretRef:
    if not ref:
        raise InvalidUsageError("Secret reference is empty")
    if ref.startswith(ENV_SCHEME):
        try:
            key = validate_env_name(ref[len(ENV_SCHEME) :])
        except InvalidUsageError:
            # Re-raise without the reference: `ref` may be a pasted key.
            raise InvalidUsageError(
                "Invalid environment secret reference: the variable name may "
                "only contain letters, digits and underscores, and must not "
                "start with a digit",
                hint=(
                    "Pass a variable NAME such as OPENAI_API_KEY. If you pasted "
                    "an API key, put it in the API key field instead."
                ),
            ) from None
        return SecretRef(scheme="env", key=key)
    if ref.startswith(FILE_SCHEME):
        key = ref[len(FILE_SCHEME) :]
        if not key or key.startswith(("/", "\\")):
            raise InvalidUsageError(f"Invalid secret reference: {ref}")
        if any(part in ("", ".", "..") for part in Path(key).parts):
            raise InvalidUsageError(f"Invalid secret reference: {ref}")
        return SecretRef(scheme="file", key=key)
    if ref.startswith(KEYRING_SCHEME):
        key = ref[len(KEYRING_SCHEME) :]
        if not key or key.startswith(("/", "\\")):
            raise InvalidUsageError(f"Invalid secret reference: {ref}")
        if any(part in ("", ".", "..") for part in Path(key).parts):
            raise InvalidUsageError(f"Invalid secret reference: {ref}")
        return SecretRef(scheme="keyring", key=key)
    raise InvalidUsageError(f"Unknown secret reference scheme: {ref}")


class FileCredentialStore:
    """File-backed backend under `~/.rinari/credentials/` (mode 0600 best effort)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, key: str) -> Path:
        return self.root / key

    def store(self, key: str, secret: str) -> str:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return f"{FILE_SCHEME}{key}"

    def resolve(self, key: str) -> str:
        path = self.path_for(key)
        if not path.is_file():
            raise AuthenticationRequiredError(f"Stored credential not found: {key}")
        return path.read_text(encoding="utf-8")

    def delete(self, key: str) -> bool:
        path = self.path_for(key)
        if path.is_file():
            path.unlink()
            return True
        return False

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def stage_unique(self, key: str, secret: str) -> str:
        """Write `secret` under a fresh sub-key without touching `key` itself.

        Used by the rotation protocol: the previous file is never rewritten,
        so no failure/interruption can destroy the resolvable value. The new
        copy lives in a parallel namespace (`gen/<key>/gen-<n>`) to keep
        files and directories disjoint from the base path.
        """
        n = 1
        while self.exists(f"gen/{key}/gen-{n}"):
            n += 1
        self.store(f"gen/{key}/gen-{n}", secret)
        return f"{FILE_SCHEME}gen/{key}/gen-{n}"


def _keyring_backend() -> Any | None:
    """Return the keyring module iff an OS backend is functional, else None.

    Headless/CI machines (and `RINARI_KEYRING=0`) fall back to file storage.
    """
    if os.environ.get(KEYRING_ENV_DISABLE) == "0":
        return None
    try:
        import keyring
    except ImportError:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:
        return None
    if backend is None or type(backend).__module__.startswith("keyring.backends.fail"):
        return None
    return keyring


class KeyringCredentialStore:
    """OS-native backend: Windows Credential Manager, macOS Keychain, or
    Secret Service via the `keyring` package; the backend object is
    duck-typed (set_password/get_password/delete_password) so tests inject
    fakes.

    Two ordering constraints shape this class (see the CredWrite error 8
    report and the PR review):

    * The Windows backend of `keyring` keeps one credential per service and
      displaces the previous one to ``<username>@<service>`` on every write,
      so sharing a service name orphaned one vault entry per write until the
      vault filled up. Every key therefore gets its own service name, scoped
      by home (``rinari/<home>/providers/<id>``) so a cleanup run from another
      home cannot touch these credentials.
    * No phase may destroy the last copy of a secret. A rotation writes the
      new value to a staging entry, copies the current value to a previous
      entry, and only then replaces the definitive target, which is deleted
      immediately before the write to avoid a displaced leftover. A failure
      anywhere leaves the previous value resolvable and the new one staged.
    """

    SERVICE = "rinari"

    def __init__(
        self,
        backend: Any,
        *,
        scope: str | None = None,
        lock_path: Path | None = None,
    ) -> None:
        self._backend = backend
        self._scope = scope
        self._lock_path = Path(lock_path) if lock_path is not None else None

    def _locked(self) -> Any:
        """Serializa mutaciones del vault entre procesos, si hay lock configurado."""
        if self._lock_path is None:
            return contextlib.nullcontext()
        return file_lock(self._lock_path)

    # -- naming ----------------------------------------------------------
    def _prefix(self) -> str:
        return f"{self.SERVICE}/{self._scope}" if self._scope else self.SERVICE

    def _service(self, key: str) -> str:
        return f"{self._prefix()}/{key}"

    def _staging_service(self, key: str) -> str:
        return f"{self._prefix()}/staging/{key}"

    def _previous_service(self, key: str) -> str:
        return f"{self._prefix()}/previous/{key}"

    def _unscoped_service(self, key: str) -> str:
        return f"{self.SERVICE}/{key}"

    def _readable_services(self, key: str) -> tuple[str, ...]:
        """Orden de resolución: vigente, anterior, sin scope, legado, staging.

        El staging va al final a propósito: es una copia en vuelo, no un valor
        en vigor; solo resuelve si no queda ninguna otra copia (por ejemplo si
        se interrumpió una rotación antes del reemplazo definitivo).
        """
        services = [self._service(key), self._previous_service(key)]
        if self._scope:
            services.append(self._unscoped_service(key))
        services.extend([self.SERVICE, self._staging_service(key)])
        return tuple(services)

    # -- backend helpers -------------------------------------------------
    def _read(self, service: str, key: str) -> str | None:
        try:
            value = self._backend.get_password(service, key)
        except Exception:
            return None
        return value or None

    def _drop(self, service: str, key: str) -> bool:
        try:
            self._backend.delete_password(service, key)
        except Exception:
            return False
        return True

    def _write_verified(self, service: str, key: str, secret: str, *, action: str) -> None:
        # Borrar primero deja el slot limpio: sin desplazamiento
        # (`<usuario>@<servicio>`) no quedan huérfanas. Solo se usa con copias
        # transitorias o justo antes del reemplazo definitivo.
        self._drop(service, key)
        try:
            self._backend.set_password(service, key, secret)
        except Exception as exc:
            raise CredentialWriteError(
                f"Could not {action} in the OS credential store: {exc}",
                hint=(
                    "The OS credential store may be full or locked. Run "
                    "`rinari secrets cleanup --apply` and retry "
                    "`rinari providers auth <alias>`; the previous value is kept."
                ),
                details={"service": service, "phase": action},
            ) from exc
        if self._read(service, key) != secret:
            raise CredentialWriteError(
                f"The OS credential store did not return the value it just tried to {action}",
                hint=(
                    "Run `rinari secrets cleanup --apply` to check a full vault; "
                    "the previous value is kept."
                ),
                details={"service": service, "phase": f"{action}:verification"},
            )

    def _restore(self, key: str, secret: str | None) -> None:
        """Devuelve el valor anterior al destino definitivo, si se puede.

        Se usa cuando la escritura definitiva no quedó verificable: el valor
        vigente sigue siendo el anterior (la rotación no se confirmó) y no debe
        quedar un valor corrupto ocupando el destino.
        """
        if secret is None:
            return
        with contextlib.suppress(Exception):
            self._drop(self._service(key), key)
            self._backend.set_password(self._service(key), key, secret)

    def _effective_value(self, key: str) -> str | None:
        """Valor en vigor, con el mismo orden que `resolve`."""
        for service in self._readable_services(key):
            value = self._read(service, key)
            if value is not None:
                return value
        return None

    # -- protocol --------------------------------------------------------
    def store(self, key: str, secret: str) -> str:
        with self._locked():
            return self._rotate(key, secret)

    def _rotate(self, key: str, secret: str) -> str:
        last_known = self._effective_value(key)

        # 1) La copia nueva, verificada, en staging: no toca el valor vigente.
        self._write_verified(self._staging_service(key), key, secret, action="stage the new secret")
        # 2) Copia del valor anterior, solo si el backup no la tiene ya: si el
        #    backup ES la última copia válida, reescribirlo la retiraría un
        #    instante y un fallo ahí la perdería.
        if last_known is not None and self._read(self._previous_service(key), key) != last_known:
            self._write_verified(
                self._previous_service(key), key, last_known, action="back up the current secret"
            )
        # 3) Reemplazo definitivo. El valor vigente ya está a salvo en `previous`
        #    (o no existía), así que borrar el destino antes de escribir no
        #    elimina la última copia. Si no queda verificable, se devuelve el
        #    valor anterior al destino antes de propagar el error.
        try:
            self._write_verified(self._service(key), key, secret, action="store the secret")
        except CredentialWriteError:
            self._restore(key, last_known)
            raise
        # 4) Confirmado: recién ahora se retiran los transitorios (el valor nuevo
        #    ya quedó verificado en el destino definitivo).
        self._drop(self._staging_service(key), key)
        self._drop(self._previous_service(key), key)
        return f"{KEYRING_SCHEME}{key}"

    def staged(self, key: str) -> str | None:
        """Valor nuevo en vuelo, recuperable tras una rotación fallida."""
        return self._read(self._staging_service(key), key)

    def stage_unique(self, key: str, secret: str) -> str:
        """Write `secret` under a fresh service-name slot, non-destructively.

        The current entry (`rinari/<home>/providers/<id>`) is never touched,
        so an interruption can never destroy the resolvable value; the new
        one lands under `rinari/<home>/gen/<id>/gen-<n>`. Resolution is not
        affected: the record only points to `keyring://providers/<id>/gen-N`
        after the SQL commit.
        """
        with self._locked():
            n = 1
            while self._read(self._service(f"gen/{key}/gen-{n}"), f"gen/{key}/gen-{n}") is not None:
                n += 1
            slot = f"gen/{key}/gen-{n}"
            self._write_verified(self._service(slot), slot, secret, action="stage the new secret")
            return f"{KEYRING_SCHEME}{slot}"

    def resolve(self, key: str) -> str:
        for service in self._readable_services(key):
            value = self._read(service, key)
            if value is not None:
                return value
        raise AuthenticationRequiredError(
            f"Stored credential not found: {key}",
            hint="Store it again with `rinari secrets add <provider>`.",
        )

    def delete(self, key: str) -> bool:
        # Sin cortocircuito: hay que visitar todos los nombres gestionados.
        with self._locked():
            removed = False
            for service in self._readable_services(key):
                removed = self._drop(service, key) or removed
            return removed

    def exists(self, key: str) -> bool:
        # Cleanup must distinguish an inaccessible vault from an absent entry.
        return any(
            self._backend.get_password(service, key) is not None
            for service in self._readable_services(key)
        )


class CredentialStore:
    """Facade dispatching secret references to their backend.

    Environment references are read-only: Rinari never creates or clears
    environment variables.
    """

    def __init__(
        self,
        layout: HomeLayout,
        env: Mapping[str, str] | None = None,
        keyring_backend: Any | None = "auto",
        scope: str | None = None,
    ) -> None:
        """Select the preferred secret backend for new writes.

        ``"auto"`` uses the OS store when functional (file fallback);
        ``None`` disables it (file only — hermetic tests); any other object
        is used as the keyring backend directly (injected fakes in tests).

        ``scope`` is the stable home identifier used to namespace OS-store
        entries; it defaults to the home's own identifier so two homes sharing
        an OS account never collide (or clean up each other's secrets).
        """
        self.files = FileCredentialStore(layout.credentials_dir)
        self._env: Mapping[str, str] = env if env is not None else os.environ
        self.scope = home_identifier(layout.root) if scope is None else scope
        self.lock_path = layout.credentials_lock
        if keyring_backend is None:
            self.keyring: KeyringCredentialStore | None = None
        elif isinstance(keyring_backend, str) and keyring_backend == "auto":
            detected = _keyring_backend()
            self.keyring = (
                KeyringCredentialStore(detected, scope=self.scope, lock_path=self.lock_path)
                if detected is not None
                else None
            )
        else:
            self.keyring = KeyringCredentialStore(
                keyring_backend, scope=self.scope, lock_path=self.lock_path
            )

    def provider_secret_ref(self, provider_id: str) -> str:
        return f"{FILE_SCHEME}providers/{provider_id}"

    def stage_unique_provider_secret(
        self, provider_id: str, secret: str, *, before_write=None
    ) -> str:
        """Write `secret` under a FRESH per-provider reference, non-destructively.

        Rotation protocol (review P1: interrupt-safe credential rotation):
        the previous value is never overwritten — the new one lands under an
        independent reference (`providers/<id>/gen-<n>`), the confirmed record
        points at it after the SQL commit, and only then is the previous
        reference retired. Any failure/interruption before the commit leaves
        the previous reference intact (candidate cleaned by `rinari secrets
        cleanup`); a commit followed by a crash before the retire leaves the
        previous copy as one orphan, resolvable via the old reference until
        cleanup removes it — never a lost credential.
        """
        if before_write is not None:
            import uuid

            key = f"gen/providers/{provider_id}/gen-{uuid.uuid4().int}"
            ref = f"{KEYRING_SCHEME if self.keyring is not None else FILE_SCHEME}{key}"
            before_write(ref)
            if self.keyring is not None:
                with self.keyring._locked():
                    self.keyring._write_verified(
                        self.keyring._service(key), key, secret, action="stage secret"
                    )
            else:
                self.files.store(key, secret)
            return ref
        if self.keyring is not None:
            return self.keyring.stage_unique(f"providers/{provider_id}", secret)
        return self.files.stage_unique(f"providers/{provider_id}", secret)

    def store_provider_secret(self, provider_id: str, secret: str) -> str:
        if self.keyring is not None:
            return self.keyring.store(f"providers/{provider_id}", secret)
        return self.files.store(f"providers/{provider_id}", secret)

    def resolve(self, ref: str) -> str:
        parsed = parse_secret_ref(ref)
        if parsed.scheme == "env":
            value = self._env.get(parsed.key)
            if not value:
                raise AuthenticationRequiredError(
                    f"Environment variable {parsed.key} is not set",
                    hint=(
                        f"Set {parsed.key} in the environment or run "
                        f"`rinari providers auth` with another reference."
                    ),
                )
            return value
        if parsed.scheme == "keyring":
            if self.keyring is None:
                raise AuthenticationRequiredError(
                    f"OS credential store unavailable for: {parsed.key}"
                )
            return self.keyring.resolve(parsed.key)
        return self.files.resolve(parsed.key)

    def delete(self, ref: str) -> bool:
        parsed = parse_secret_ref(ref)
        if parsed.scheme == "env":
            return False
        if parsed.scheme == "keyring":
            return self.keyring.delete(parsed.key) if self.keyring is not None else False
        return self.files.delete(parsed.key)

    def exists(self, ref: str) -> bool:
        parsed = parse_secret_ref(ref)
        if parsed.scheme == "env":
            return bool(self._env.get(parsed.key))
        if parsed.scheme == "keyring":
            if self.keyring is None:
                raise AuthenticationRequiredError("OS credential store unavailable")
            return self.keyring.exists(parsed.key)
        return self.files.exists(parsed.key)
