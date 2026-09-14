"""Identificador de home: estable, único por home y atómico entre procesos."""

from __future__ import annotations

import threading

from rinari.shared import paths
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

    def first_read_misses(path, *, attempts=1):
        # Fuerza el escenario del reporte: ambos leen "ausente" antes de crear.
        if not getattr(local, "missed", False):
            local.missed = True
            barrier.wait(timeout=5)
            return None
        return real_read(path, attempts=attempts)

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


def test_read_only_home_falls_back_to_a_deterministic_identifier(tmp_path, monkeypatch) -> None:
    root = tmp_path / "home"
    root.mkdir()

    def deny(*args, **kwargs):
        raise PermissionError("home de solo lectura")

    monkeypatch.setattr(paths.os, "open", deny)
    first = home_identifier(root)
    assert home_identifier(root) == first
    assert not (root / paths.HOME_ID_FILE).exists()
