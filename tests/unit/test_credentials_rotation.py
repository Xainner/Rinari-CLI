"""Rotación de credenciales: fallos por fase y recuperación.

Revisión del PR: los tests anteriores hacían fallar *todas* las escrituras, así
que solo cubrían el fallo inicial del staging. Este archivo cubre cada fase por
separado y exige que ninguna deje al proveedor sin ninguna copia válida:

    staging (escritura / verificación) -> copia nueva
    definitivo (escritura / verificación) -> reemplazo confirmado
    limpieza -> las copias transitorias desaparecen solo con el nuevo valor ya
                confirmado en el destino definitivo
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rinari.application.credentials import KeyringCredentialStore
from rinari.shared.errors import CredentialWriteError

_SPEC = importlib.util.spec_from_file_location(
    "test_credentials_keyring", Path(__file__).with_name("test_credentials_keyring.py")
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
SlotBackend = _MODULE.SlotBackend

SCOPE = "home-a"
KEY = "providers/prov_1"


class PhaseBackend(SlotBackend):
    """Fake que puede fallar o corromper objetivos concretos."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_on: set[str] = set()
        self.corrupt_on: set[str] = set()
        self.persistent: set[str] = set()

    def set_password(self, service: str, username: str, password: str) -> None:
        # `persistent` modela un destino que sigue fallando en los reintentos;
        # `fail_on`/`corrupt_on` son fallos de una sola vez.
        if service in self.persistent:
            raise RuntimeError("(8, 'CredWrite', 'persistente')")
        if service in self.fail_on:
            self.fail_on.discard(service)
            raise RuntimeError("(8, 'CredWrite', 'sin recursos')")
        super().set_password(service, username, password)
        if service in self.corrupt_on:
            self.corrupt_on.discard(service)
            user, _ = self.entries[service]
            self.entries[service] = (user, "corrupted")


def _store(backend: SlotBackend) -> KeyringCredentialStore:
    return KeyringCredentialStore(backend, scope=SCOPE)


def _targets(backend: SlotBackend) -> list[str]:
    return sorted(backend.entries)


def test_store_scope_separates_homes() -> None:
    backend = PhaseBackend()
    store = _store(backend)
    store.store(KEY, "one")
    assert _targets(backend) == ["rinari/home-a/providers/prov_1"]
    assert store.resolve(KEY) == "one"


def test_successful_rotation_keeps_one_entry_and_the_new_value() -> None:
    backend = PhaseBackend()
    store = _store(backend)
    store.store(KEY, "old")
    store.store(KEY, "new")
    assert _targets(backend) == ["rinari/home-a/providers/prov_1"]
    assert store.resolve(KEY) == "new"


def test_staging_write_failure_leaves_the_current_value_untouched() -> None:
    backend = PhaseBackend()
    store = _store(backend)
    store.store(KEY, "old")
    backend.fail_on = {"rinari/home-a/staging/providers/prov_1"}
    with pytest.raises(CredentialWriteError):
        store.store(KEY, "new")
    assert store.resolve(KEY) == "old"


def test_staging_verification_failure_keeps_the_current_value() -> None:
    backend = PhaseBackend()
    store = _store(backend)
    store.store(KEY, "old")
    backend.corrupt_on = {"rinari/home-a/staging/providers/prov_1"}
    with pytest.raises(CredentialWriteError):
        store.store(KEY, "new")
    assert store.resolve(KEY) == "old"


def test_definitive_write_failure_keeps_previous_and_staged_copies() -> None:
    """El caso del revisor: solo falla la escritura definitiva."""
    backend = PhaseBackend()
    store = _store(backend)
    store.store(KEY, "old")
    backend.fail_on = {"rinari/home-a/providers/prov_1"}
    with pytest.raises(CredentialWriteError):
        store.store(KEY, "new")

    # Nunca cero copias: la anterior sigue vigente y la nueva queda recuperable.
    assert store.exists(KEY) is True
    assert store.resolve(KEY) == "old"
    assert store.staged(KEY) == "new"


def test_definitive_verification_failure_restores_the_previous_value() -> None:
    backend = PhaseBackend()
    store = _store(backend)
    store.store(KEY, "old")
    backend.corrupt_on = {"rinari/home-a/providers/prov_1"}
    with pytest.raises(CredentialWriteError):
        store.store(KEY, "new")

    assert store.resolve(KEY) == "old"
    assert backend.entries["rinari/home-a/providers/prov_1"][1] == "old"
    assert store.staged(KEY) == "new"


def test_retry_after_a_failed_rotation_completes_and_cleans_up() -> None:
    backend = PhaseBackend()
    store = _store(backend)
    store.store(KEY, "old")
    backend.fail_on = {"rinari/home-a/providers/prov_1"}
    with pytest.raises(CredentialWriteError):
        store.store(KEY, "new")
    backend.fail_on = set()
    store.store(KEY, "new")

    assert _targets(backend) == ["rinari/home-a/providers/prov_1"]
    assert store.resolve(KEY) == "new"
    assert store.staged(KEY) is None


def test_resolution_order_current_then_previous_then_legacy_then_staging() -> None:
    backend = PhaseBackend()
    store = _store(backend)
    store.store(KEY, "current")
    backend.entries["rinari/home-a/previous/providers/prov_1"] = (KEY, "previous")
    backend.entries["rinari"] = (KEY, "legacy")
    backend.entries["rinari/home-a/staging/providers/prov_1"] = (KEY, "staged")
    assert store.resolve(KEY) == "current"

    del backend.entries["rinari/home-a/providers/prov_1"]
    assert store.resolve(KEY) == "previous"

    del backend.entries["rinari/home-a/previous/providers/prov_1"]
    backend.entries["rinari/providers/prov_1"] = (KEY, "unscoped")
    assert store.resolve(KEY) == "unscoped"

    del backend.entries["rinari/providers/prov_1"]
    assert store.resolve(KEY) == "legacy"

    del backend.entries["rinari"]
    assert store.resolve(KEY) == "staged"


def test_delete_removes_every_managed_copy() -> None:
    backend = PhaseBackend()
    store = _store(backend)
    store.store(KEY, "current")
    backend.entries["rinari/home-a/previous/providers/prov_1"] = (KEY, "previous")
    backend.entries["rinari/home-a/staging/providers/prov_1"] = (KEY, "staged")
    backend.entries["rinari/providers/prov_1"] = (KEY, "unscoped")
    backend.entries["rinari"] = (KEY, "legacy")

    assert store.delete(KEY) is True
    assert backend.entries == {}
    assert store.delete(KEY) is False


def test_two_homes_do_not_collide_in_the_same_backend() -> None:
    backend = PhaseBackend()
    first = KeyringCredentialStore(backend, scope="home-a")
    second = KeyringCredentialStore(backend, scope="home-b")
    first.store(KEY, "one")
    second.store(KEY, "two")
    assert first.resolve(KEY) == "one"
    assert second.resolve(KEY) == "two"
    assert _targets(backend) == [
        "rinari/home-a/providers/prov_1",
        "rinari/home-b/providers/prov_1",
    ]


def test_persistent_definitive_failure_keeps_the_previous_value_every_time() -> None:
    """Repro de la segunda revisión: el destino sigue fallando en varios intentos."""
    backend = PhaseBackend()
    store = _store(backend)
    store.store(KEY, "old")
    backend.persistent = {"rinari/home-a/providers/prov_1"}

    for attempt in range(3):
        with pytest.raises(CredentialWriteError):
            store.store(KEY, f"attempt-{attempt}")
        assert store.resolve(KEY) == "old"


def test_retry_does_not_rewrite_the_backup_holding_the_last_copy() -> None:
    """Con el backup intacto, el reintento no lo reescribe: un fallo del backup
    ya no puede llevarse la última copia válida (repro de la revisión)."""
    backend = PhaseBackend()
    store = _store(backend)
    store.store(KEY, "old")
    backend.persistent = {"rinari/home-a/providers/prov_1"}
    with pytest.raises(CredentialWriteError):
        store.store(KEY, "new")
    assert store.resolve(KEY) == "old"

    # El destino ya responde; el backup falla, pero no hace falta reescribirlo.
    backend.persistent = {"rinari/home-a/previous/providers/prov_1"}
    store.store(KEY, "new2")

    assert store.resolve(KEY) == "new2"
    assert _targets(backend) == ["rinari/home-a/providers/prov_1"]


def test_backup_write_failure_keeps_the_current_value() -> None:
    backend = PhaseBackend()
    store = _store(backend)
    store.store(KEY, "old")
    backend.persistent = {"rinari/home-a/previous/providers/prov_1"}
    with pytest.raises(CredentialWriteError):
        store.store(KEY, "new")

    assert store.resolve(KEY) == "old"
    assert "rinari/home-a/providers/prov_1" in backend.entries


def test_recovery_after_consecutive_failures_ends_with_one_entry() -> None:
    backend = PhaseBackend()
    store = _store(backend)
    store.store(KEY, "old")
    backend.persistent = {"rinari/home-a/providers/prov_1"}
    with pytest.raises(CredentialWriteError):
        store.store(KEY, "new")
    backend.persistent = {"rinari/home-a/previous/providers/prov_1"}
    store.store(KEY, "new2")
    backend.persistent = set()
    store.store(KEY, "new3")

    assert _targets(backend) == ["rinari/home-a/providers/prov_1"]
    assert store.resolve(KEY) == "new3"
    assert store.staged(KEY) is None
