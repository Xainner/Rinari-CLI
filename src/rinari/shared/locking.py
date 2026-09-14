"""Exclusión entre procesos para mutaciones del vault de credenciales.

El vault es global al usuario de Windows (o al keychain del sistema) mientras
que las bases de datos y el namespace de credenciales son por home: dos
procesos de Rinari -o una limpieza corriendo junto a un alta- pueden pisarse.
Este lock compartido serializa esas mutaciones.

Solo stdlib: `msvcrt.locking` en Windows, `fcntl.flock` en POSIX. Los locks de
ambos APIs son por descriptor abierto, así que dos handles del mismo proceso
también se excluyen (lo que hace testeable la exclusión con hilos).
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

if os.name == "nt":  # pragma: no cover - se elige por plataforma
    import msvcrt
else:  # pragma: no cover - se elige por plataforma
    import fcntl


from rinari.shared.errors import LockTimeoutError

_HELD = threading.local()


@contextmanager
def file_lock(path: Path, *, timeout: float = 5.0, poll: float = 0.05) -> Iterator[None]:
    """Lock exclusivo entre procesos sobre `path` (se crea si no existe).

    Reentrante dentro del mismo hilo: envolver una transacción que por dentro
    vuelve a pedir el lock (por ejemplo una escritura de credencial) no se
    auto-bloquea. Otros hilos y otros procesos sí esperan.
    """
    key = str(Path(path).resolve())
    held: dict[str, int] = getattr(_HELD, "paths", None) or {}
    _HELD.paths = held
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 - se cierra en el finally
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                _acquire(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"Could not acquire {path.name} within {timeout:g}s",
                        hint=(
                            "Another Rinari process is writing credentials; retry once it finishes."
                        ),
                    ) from None
                time.sleep(poll)
        held[key] = 1
        try:
            yield
        finally:
            held.pop(key, None)
    finally:
        with contextlib.suppress(OSError):
            _release(handle)
        handle.close()


def _acquire(handle: IO[bytes]) -> None:
    if os.name == "nt":  # pragma: no cover - plataforma
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - plataforma
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(handle: IO[bytes]) -> None:
    if os.name == "nt":  # pragma: no cover - plataforma
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - plataforma
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
