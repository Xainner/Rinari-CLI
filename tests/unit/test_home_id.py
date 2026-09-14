"""Identificador de home: uno solo por home, publicado completo.

Revisión del PR (P1): con O_EXCL el ganador creaba un archivo vacío y escribía
el UUID después; un segundo proceso que no lo veía a tiempo devolvía el hash de
la ruta y quedaban dos namespaces distintos para el mismo home. La publicación
ahora se serializa con un lock y usa un reemplazo atómico, y ante timeout se
propaga un error reintentable en vez de un identificador alternativo.
"""

from __future__ import annotations

import threading
import time

from rinari.shared import paths
from rinari.shared.errors import HomeIdUnavailableError
from rinari.shared.locking import file_lock
from rinari.shared.paths import home_identifier


def test_identifier_is_stable_and_persisted(tmp_path) -> None:
    root = tmp_path / "home"
    first = home_identifier(root)
    second = home_identifier(root)
    assert first == second
    assert (root / paths.HOME_ID_FILE).read_text(encoding="utf-8").strip() == first


def test_different_homes_get_different_identifiers(tmp_path) -> None:
    assert home_identifier(tmp_path / "a") != home_identifier(tmp_path / "b")


def test_concurrent_creation_converges_on_the_persisted_identifier(tmp_path, monkeypatch) -> None:
    root = tmp_path / "home"
    root.mkdir()
    real_read = paths._read_home_id
    barrier = threading.Barrier(2)
    local = threading.local()

    def first_read_misses(path, **kwargs):
        # Fuerza el escenario del reporte: ambos leen "ausente" antes de crear.
        if not getattr(local, "missed", False):
            local.missed = True
            barrier.wait(timeout=5)
            return None
        return real_read(path, **kwargs)

    monkeypatch.setattr(paths, "_read_home_id", first_read_misses)

    results: list[str] = []

    def run() -> None:
        results.append(home_identifier(root))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(results) == 2
    assert results[0] == results[1]
    assert (root / paths.HOME_ID_FILE).read_text(encoding="utf-8").strip() == results[0]


def test_waits_for_a_slow_publisher_and_uses_its_identifier(tmp_path) -> None:
    """Publicación lenta: el segundo proceso espera y toma el id del primero."""
    root = tmp_path / "home"
    root.mkdir()
    published = threading.Event()
    release = threading.Event()

    def publisher() -> None:
        with file_lock(root / paths.HOME_ID_LOCK_FILE):
            (root / paths.HOME_ID_FILE).write_text("abcdef0123456789\n", encoding="utf-8")
            published.set()
            release.wait(5)

    thread = threading.Thread(target=publisher)
    thread.start()
    assert published.wait(5)
    try:
        assert home_identifier(root) == "abcdef0123456789"
    finally:
        release.set()
        thread.join(5)


def test_timeout_raises_a_retryable_error_instead_of_an_alternative_id(
    tmp_path, monkeypatch
) -> None:
    """Repro de la revisión: nunca un namespace alternativo mientras se publica."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(paths, "HOME_ID_LOCK_TIMEOUT", 0.3)

    ready = threading.Event()
    release = threading.Event()

    def slow_publisher() -> None:
        with file_lock(root / paths.HOME_ID_LOCK_FILE):
            ready.set()
            release.wait(5)

    thread = threading.Thread(target=slow_publisher)
    thread.start()
    assert ready.wait(5)
    started = time.monotonic()
    try:
        try:
            home_identifier(root)
        except HomeIdUnavailableError as error:
            assert error.machine_code == "HOME_ID_UNAVAILABLE"
            assert error.retryable is True
        else:  # pragma: no cover - la ausencia de error es el fallo
            raise AssertionError("no debe devolverse un identificador alternativo")
        assert time.monotonic() - started >= 0.25
    finally:
        release.set()
        thread.join(5)

    # El archivo tampoco se creó a medias: tras liberar, el id real es nuevo.
    assert not (root / paths.HOME_ID_FILE).exists()
    published = home_identifier(root)
    assert published != paths._fallback_identifier(root)


def test_recovers_after_an_interrupted_creator(tmp_path) -> None:
    """Un creador interrumpido deja a lo sumo un temporal, nunca un id a medias."""
    root = tmp_path / "home"
    root.mkdir()
    leftover = root / f"{paths.HOME_ID_FILE}.deadbeef.tmp"
    leftover.write_text("0000000000000000\n", encoding="utf-8")

    published = home_identifier(root)
    assert published != "0000000000000000"
    assert (root / paths.HOME_ID_FILE).read_text(encoding="utf-8").strip() == published
    # El identificador publicado no queda a medias ni depende del temporal.
    assert home_identifier(root) == published


def test_read_only_home_falls_back_to_a_deterministic_identifier(tmp_path, monkeypatch) -> None:
    root = tmp_path / "home"
    root.mkdir()

    def deny(*args, **kwargs):
        raise PermissionError("home de solo lectura")

    monkeypatch.setattr(paths.os, "replace", deny)
    first = home_identifier(root)
    assert first == paths._fallback_identifier(root)
    assert home_identifier(root) == first
    assert not (root / paths.HOME_ID_FILE).exists()
