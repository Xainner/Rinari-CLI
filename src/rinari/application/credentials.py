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

from rinari.shared.errors import AuthenticationRequiredError, InvalidUsageError
from rinari.shared.paths import HomeLayout

ENV_SCHEME = "env://"
FILE_SCHEME = "file://"

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


class CredentialStore:
    """Facade dispatching secret references to their backend.

    Environment references are read-only: Rinari never creates or clears
    environment variables.
    """

    def __init__(self, layout: HomeLayout, env: Mapping[str, str] | None = None) -> None:
        self.files = FileCredentialStore(layout.credentials_dir)
        self._env: Mapping[str, str] = env if env is not None else os.environ

    def provider_secret_ref(self, provider_id: str) -> str:
        return f"{FILE_SCHEME}providers/{provider_id}"

    def store_provider_secret(self, provider_id: str, secret: str) -> str:
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
        return self.files.resolve(parsed.key)

    def delete(self, ref: str) -> bool:
        parsed = parse_secret_ref(ref)
        if parsed.scheme == "env":
            return False
        return self.files.delete(parsed.key)

    def exists(self, ref: str) -> bool:
        parsed = parse_secret_ref(ref)
        if parsed.scheme == "env":
            return bool(self._env.get(parsed.key))
        return self.files.exists(parsed.key)
