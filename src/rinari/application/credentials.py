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
from rinari.shared.paths import HomeLayout

ENV_SCHEME = "env://"
FILE_SCHEME = "file://"
KEYRING_SCHEME = "keyring://"

#: Opt-out for the OS credential store (tests force hermetic file storage).
KEYRING_ENV_DISABLE = "RINARI_KEYRING"

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class SecretRef:
    scheme: str
    key: str


def parse_secret_ref(ref: str) -> SecretRef:
    if not ref:
        raise InvalidUsageError("Secret reference is empty")
    if ref.startswith(ENV_SCHEME):
        key = ref[len(ENV_SCHEME) :]
        if not _ENV_NAME_RE.match(key):
            raise InvalidUsageError(f"Invalid environment secret reference: {ref}")
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

    Write pattern: the Windows backend of `keyring` keeps a *single*
    credential per service and displaces the previous one to
    ``<username>@<service>`` on every write. Sharing one service name for
    every provider therefore orphaned one vault entry per write until the
    credential manager filled up and `CredWrite` failed with error 8 (see the
    CredWrite report). Each key now uses its own service name, the new value
    is written and read back through a staging entry first, and the
    definitive entry is only replaced once the new secret is durable.
    """

    SERVICE = "rinari"

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    # -- naming ----------------------------------------------------------
    def _service(self, key: str) -> str:
        return f"{self.SERVICE}/{key}"

    def _staging_service(self, key: str) -> str:
        return f"{self.SERVICE}/staging/{key}"

    def _services(self, key: str) -> tuple[str, ...]:
        # Orden de resolución: entrada actual, recuperación (staging, por si
        # una rotación no llegó a completarse) y formato legado compartido.
        return (self._service(key), self._staging_service(key), self.SERVICE)

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

    def _write_verified(self, service: str, key: str, secret: str) -> None:
        # Retirar primero deja el slot vacío: sin desplazamiento
        # (`<usuario>@<servicio>`) no quedan huérfanas.
        self._drop(service, key)
        try:
            self._backend.set_password(service, key, secret)
        except Exception as exc:
            raise CredentialWriteError(
                f"Could not write the credential to the OS store: {exc}",
                hint=(
                    "The OS credential store may be full or locked. Run "
                    "`rinari secrets cleanup --apply` to retire orphaned entries."
                ),
                details={"service": service},
            ) from exc
        if self._read(service, key) != secret:
            raise CredentialWriteError(
                "The OS credential store did not return the value it just stored",
                hint="Run `rinari secrets cleanup --apply` to check a full vault.",
                details={"service": service},
            )

    # -- protocol --------------------------------------------------------
    def store(self, key: str, secret: str) -> str:
        staging = self._staging_service(key)
        self._write_verified(staging, key, secret)
        try:
            self._write_verified(self._service(key), key, secret)
        finally:
            self._drop(staging, key)
        return f"{KEYRING_SCHEME}{key}"

    def resolve(self, key: str) -> str:
        for service in self._services(key):
            value = self._read(service, key)
            if value is not None:
                return value
        raise AuthenticationRequiredError(
            f"Stored credential not found: {key}",
            hint="Store it again with `rinari secrets add <provider>`.",
        )

    def delete(self, key: str) -> bool:
        # Sin cortocircuito: hay que visitar los tres servicios (el legado
        # también), aunque el primero ya haya borrado algo.
        removed = False
        for service in self._services(key):
            removed = self._drop(service, key) or removed
        return removed

    def exists(self, key: str) -> bool:
        return any(self._read(service, key) is not None for service in self._services(key))


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
    ) -> None:
        """Select the preferred secret backend for new writes.

        ``"auto"`` uses the OS store when functional (file fallback);
        ``None`` disables it (file only — hermetic tests); any other object
        is used as the keyring backend directly (injected fakes in tests).
        """
        self.files = FileCredentialStore(layout.credentials_dir)
        self._env: Mapping[str, str] = env if env is not None else os.environ
        if keyring_backend is None:
            self.keyring: KeyringCredentialStore | None = None
        elif isinstance(keyring_backend, str) and keyring_backend == "auto":
            detected = _keyring_backend()
            self.keyring = KeyringCredentialStore(detected) if detected is not None else None
        else:
            self.keyring = KeyringCredentialStore(keyring_backend)

    def provider_secret_ref(self, provider_id: str) -> str:
        return f"{FILE_SCHEME}providers/{provider_id}"

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
            return self.keyring.exists(parsed.key) if self.keyring is not None else False
        return self.files.exists(parsed.key)
