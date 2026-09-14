"""Regresión del patrón de escritura del store keyring.

Contexto (informe CredWrite error 8): el backend de Windows de `keyring`
mantiene **una** credencial por servicio y, en cada escritura, desplaza la
anterior a `<usuario>@<servicio>` sin limpiarla. Escribir todas las claves
bajo el servicio compartido `rinari` dejaba una entrada huérfana por alta o
rotación hasta saturar el Administrador de credenciales (~700 entradas) y
romper `CredWrite` con error 8.

El fake `SlotBackend` reproduce esa semántica exacta para que la regresión
sea verificable en cualquier plataforma.
"""

from __future__ import annotations

import pytest

from rinari.application.credentials import CredentialStore, KeyringCredentialStore
from rinari.shared.errors import CredentialWriteError
from rinari.shared.paths import layout_for


class SlotBackend:
    """Fake con la semántica observada del backend de Windows de keyring."""

    def __init__(self, *, fail_writes: bool = False, capacity: int | None = None) -> None:
        self.entries: dict[str, tuple[str, str]] = {}
        self.fail_writes = fail_writes
        self.capacity = capacity

    def _compound(self, username: str, service: str) -> str:
        return f"{username}@{service}"

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.fail_writes:
            raise RuntimeError("(8, 'CredWrite', 'No hay suficientes recursos')")
        if (
            self.capacity is not None
            and len(self.entries) >= self.capacity
            and service not in self.entries
        ):
            raise RuntimeError("(8, 'CredWrite', 'No hay suficientes recursos')")
        existing = self.entries.pop(service, None)
        if existing is not None:
            self.entries[self._compound(existing[0], service)] = existing
        self.entries[service] = (username, password)

    def get_password(self, service: str, username: str) -> str | None:
        entry = self.entries.get(service)
        if entry is not None and entry[0] == username:
            return entry[1]
        return None

    def delete_password(self, service: str, username: str) -> None:
        removed = False
        for target in (service, self._compound(username, service)):
            entry = self.entries.get(target)
            if entry is not None and entry[0] == username:
                del self.entries[target]
                removed = True
        if not removed:
            raise KeyError("(1168, 'CredDelete', 'No se ha encontrado el elemento.')")


def _displaced(backend: SlotBackend) -> list[str]:
    """Entradas huérfanas: las que quedaron desplazadas a `<usuario>@<servicio>`."""
    return [target for target in backend.entries if "@" in target]


def test_store_leaves_no_displaced_entries_on_rotation() -> None:
    backend = SlotBackend()
    store = KeyringCredentialStore(backend)
    store.store("providers/prov_1", "first")
    store.store("providers/prov_1", "second")
    assert store.resolve("providers/prov_1") == "second"
    assert _displaced(backend) == []


def test_store_of_second_provider_does_not_displace_the_first() -> None:
    backend = SlotBackend()
    store = KeyringCredentialStore(backend)
    store.store("providers/prov_1", "one")
    store.store("providers/prov_2", "two")
    assert store.resolve("providers/prov_1") == "one"
    assert store.resolve("providers/prov_2") == "two"
    assert _displaced(backend) == []


def test_store_cleans_up_its_staging_entry() -> None:
    backend = SlotBackend()
    store = KeyringCredentialStore(backend)
    store.store("providers/prov_1", "one")
    assert len(backend.entries) == 1


def test_rotation_keeps_previous_secret_when_the_write_fails() -> None:
    backend = SlotBackend()
    store = KeyringCredentialStore(backend)
    store.store("providers/prov_1", "first")
    backend.fail_writes = True
    with pytest.raises(CredentialWriteError):
        store.store("providers/prov_1", "second")
    assert store.resolve("providers/prov_1") == "first"


def test_full_vault_raises_a_structured_error_with_a_hint() -> None:
    # Capacidad para una sola credencial (staging + definitiva) en el fake.
    backend = SlotBackend(capacity=2)
    store = KeyringCredentialStore(backend)
    store.store("providers/prov_1", "one")
    with pytest.raises(CredentialWriteError) as info:
        store.store("providers/prov_2", "two")
    assert info.value.machine_code == "CREDENTIAL_STORE_WRITE_FAILED"
    assert "cleanup" in (info.value.hint or "")


def test_resolve_falls_back_to_the_legacy_shared_service() -> None:
    backend = SlotBackend()
    backend.entries["rinari"] = ("providers/prov_legacy", "legacy-secret")
    store = KeyringCredentialStore(backend)
    assert store.resolve("providers/prov_legacy") == "legacy-secret"
    assert store.exists("providers/prov_legacy")


def test_delete_removes_current_legacy_and_staging_entries() -> None:
    backend = SlotBackend()
    store = KeyringCredentialStore(backend)
    store.store("providers/prov_1", "one")
    backend.entries["rinari"] = ("providers/prov_1", "legacy")
    assert store.delete("providers/prov_1") is True
    assert backend.entries == {}
    assert store.delete("providers/prov_1") is False


def test_facade_surfaces_write_failures(tmp_path) -> None:
    backend = SlotBackend(fail_writes=True)
    store = CredentialStore(layout_for(tmp_path / "home"), keyring_backend=backend)
    with pytest.raises(CredentialWriteError):
        store.store_provider_secret("prov_1", "secret")


def test_facade_roundtrip_with_keyring_backend(tmp_path) -> None:
    backend = SlotBackend()
    store = CredentialStore(layout_for(tmp_path / "home"), keyring_backend=backend)
    ref = store.store_provider_secret("prov_1", "secret")
    assert ref == "keyring://providers/prov_1"
    assert store.resolve(ref) == "secret"
    assert store.exists(ref)
    assert store.delete(ref)
    assert not store.exists(ref)
