"""Lock compartido de credenciales: exclusión entre procesos e hilos."""

from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

from rinari.application.credentials import KeyringCredentialStore
from rinari.shared.errors import LockTimeoutError
from rinari.shared.locking import file_lock

_SPEC = importlib.util.spec_from_file_location(
    "test_credentials_keyring", Path(__file__).with_name("test_credentials_keyring.py")
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
SlotBackend = _MODULE.SlotBackend


def _hold_in_a_thread(path, *, timeout: float = 5.0):
    """Mantiene el lock en otro hilo y devuelve (hilo, release)."""
    ready = threading.Event()
    release = threading.Event()

    def run() -> None:
        with file_lock(path, timeout=timeout):
            ready.set()
            release.wait(timeout)

    thread = threading.Thread(target=run)
    thread.start()
    assert ready.wait(timeout)
    return thread, release


def _attempt_in_a_thread(path, *, timeout: float):
    """Intenta tomar el lock desde otro hilo: devuelve la excepción o None."""
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            with file_lock(path, timeout=timeout):
                outcome.append(None)
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(10)
    return outcome[0]


def test_lock_excludes_another_thread(tmp_path) -> None:
    path = tmp_path / "credentials.lock"
    holder, release = _hold_in_a_thread(path)
    try:
        error = _attempt_in_a_thread(path, timeout=0.2)
        assert isinstance(error, LockTimeoutError)
    finally:
        release.set()
        holder.join(5)
    # Liberado: se puede volver a tomar.
    with file_lock(path, timeout=0.2):
        pass


def test_lock_is_reentrant_within_the_same_thread(tmp_path) -> None:
    path = tmp_path / "credentials.lock"
    with file_lock(path, timeout=0.2), file_lock(path, timeout=0.2):
        pass
    assert _attempt_in_a_thread(path, timeout=0.2) is None


def test_lock_timeout_is_a_structured_retryable_error(tmp_path) -> None:
    path = tmp_path / "credentials.lock"
    holder, release = _hold_in_a_thread(path)
    try:
        error = _attempt_in_a_thread(path, timeout=0.1)
    finally:
        release.set()
        holder.join(5)
    assert isinstance(error, LockTimeoutError)
    assert error.machine_code == "CREDENTIAL_STORE_BUSY"
    assert "retry" in (error.hint or "").lower()
    assert error.retryable is True


def test_store_waits_for_the_lock_instead_of_writing(tmp_path) -> None:
    backend = SlotBackend()
    lock_path = tmp_path / "credentials.lock"
    store = KeyringCredentialStore(backend, scope="home-a", lock_path=lock_path)

    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with file_lock(lock_path):
            holding.set()
            release.wait(5)

    def writer() -> None:
        store.store("providers/prov_1", "one")

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    assert holding.wait(5)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    time.sleep(0.3)
    # Mientras el lock está tomado por otro proceso/hilo, no se escribe nada.
    assert backend.entries == {}

    release.set()
    writer_thread.join(5)
    holder_thread.join(5)

    assert store.resolve("providers/prov_1") == "one"
    assert sorted(backend.entries) == ["rinari/home-a/providers/prov_1"]
