"""Contextos de browser por sesión (documento 03 §4.1).

La regla que este módulo existe para hacer cumplir: **la registry pertenece al
Engine, no al modelo**. El contexto de una sesión se crea desde configuración y
capabilities del host confiable, y el `context_id` se acuña aquí. Un argumento
de herramienta no puede elegir backend, host ni `webContentsId` para salirse de
su sesión: las herramientas nunca nombran un contexto, sólo usan el de la
sesión en la que corren.

También decide **cuándo** una sesión usa el browser nativo. El anuncio de la
capability en el hello señala soporte del protocolo, no que ya exista una
vista (§5.2): hace falta soporte + registro activo + contexto listo.
"""

from __future__ import annotations

import contextlib
import secrets
import threading
from pathlib import Path
from typing import Any

from rinari.browser.host_backend import HostBackend
from rinari.engine_protocol.browser_host import BrowserHostBridge


class BrowserRegistry:
    """Un contexto por sesión, con el backend que le corresponda."""

    def __init__(self, bridge: BrowserHostBridge, home_root: Path) -> None:
        self._bridge = bridge
        self._home_root = Path(home_root)
        self._lock = threading.Lock()
        self._contexts: dict[str, dict[str, Any]] = {}

    def native_available(self) -> bool:
        """¿Puede una sesión nueva usar el browser del escritorio?

        Soporte del protocolo y registro activo. Que el contexto esté listo se
        comprueba al crearlo, no aquí.
        """
        return self._bridge.available and "browser_native_view_v1" in self._bridge.capabilities()

    def acquire(self, session_id: str) -> Any | None:
        """Devuelve el `BrowserManager` de la sesión, creándolo si procede.

        `None` cuando no hay browser nativo disponible: quien llama decide si
        cae al backend externo o informa de que no hay browser. No se inventa
        aquí un fallback silencioso, porque cambiar de backend a mitad de
        sesión es justo lo que el §1 prohíbe.
        """
        from rinari.browser.manager import BrowserManager

        with self._lock:
            existing = self._contexts.get(session_id)
            if existing is not None:
                if not existing["manager"].is_disposed:
                    # Se devuelve aunque el host se haya ido. Un contexto que
                    # empezó nativo sigue siéndolo hasta que se cierre: si se
                    # sustituyera por el backend externo, la sesión abriría
                    # otro navegador con la misma URL y creería que es el suyo,
                    # que es justo lo que el §1 prohíbe. Sin host, las
                    # operaciones fallan con BROWSER_DISCONNECTED, que es
                    # información verdadera.
                    return existing["manager"]
                # Dispuesto: se retira en vez de devolverse. Entregar un
                # manager cerrado hacía que una sesión reabierta creyera tener
                # browser mientras cada operación fallaba.
                self._contexts.pop(session_id, None)

            if not self.native_available():
                return None

            # El id lo acuña el componente confiable y se mapea a objetos
            # internos; nunca viaja un identificador elegido fuera (§5.3). Una
            # reapertura acuña uno nuevo: el contexto anterior queda cerrado y
            # su backend rechaza lo que llegue tarde a su nombre.
            context_id = secrets.token_hex(16)
            backend = HostBackend(self._bridge, session_id=session_id, context_id=context_id)
            manager = BrowserManager(
                session_id=session_id,
                home_root=self._home_root,
                backend=backend,
            )
            self._contexts[session_id] = {"manager": manager, "context_id": context_id}
            return manager

    def set_control(
        self, session_id: str, owner: str, expected_revision: int | None = None
    ) -> dict[str, Any]:
        """Transición de control de la sesión (§7, `browser.control.set`)."""
        with self._lock:
            entry = self._contexts.get(session_id)
        if entry is None:
            from rinari.browser.manager import BrowserError

            raise BrowserError(
                "BROWSER_DISCONNECTED", "this session has no desktop browser context"
            )
        backend = entry["manager"]._backend
        return backend.set_control(owner, expected_revision=expected_revision)

    def release(self, session_id: str) -> None:
        """Cierra el contexto de una sesión. Idempotente."""
        with self._lock:
            entry = self._contexts.pop(session_id, None)
        if entry is not None:
            self._bridge.forget(entry["context_id"])
            # Cerrar una sesión no falla porque el host ya no esté.
            with contextlib.suppress(Exception):
                entry["manager"].close()

    def release_all(self) -> None:
        with self._lock:
            entries = list(self._contexts.values())
            self._contexts.clear()
        for entry in entries:
            with contextlib.suppress(Exception):
                entry["manager"].close()

    def describe(self, session_id: str) -> dict[str, Any]:
        """Metadata pública para `browser.context.get`.

        Sin endpoints, sin cookies, sin ids del host: el §5.2 acota esto a
        «metadata segura y disponibilidad».
        """
        supported = (
            self._bridge.available and "browser_native_view_v1" in self._bridge.capabilities()
        )
        with self._lock:
            entry = self._contexts.get(session_id)

        if entry is None:
            return {
                "supported": supported,
                "host_registered": self._bridge.available,
                "available": supported,
                "backend": "electron-native" if supported else None,
                "context_state": "absent",
                "state": "absent",
                "targets": [],
                "active_target_id": None,
            }

        manager = entry["manager"]
        backend = manager._backend
        observed = self._bridge.observed(entry["context_id"])
        targets = observed.get("targets") or []

        # `ready` exige contexto **y** una página lista. Anunciarlo sólo por
        # tener binding haría que la UI mostrara un browser que aún no puede
        # enseñar nada (§5.2: soporte + registro + contexto listo).
        if manager.is_disposed:
            state = "disposed"
        elif not manager.connected:
            state = "disconnected"
        elif targets:
            state = "ready"
        else:
            state = "creating"

        return {
            "supported": supported,
            "host_registered": self._bridge.available,
            "available": state == "ready",
            "backend": "electron-native",
            "context_state": state,
            # Se conserva `state` por compatibilidad con el consumidor actual.
            "state": state,
            "context_id": entry["context_id"],
            "control": backend.control,
            "control_revision": backend.status().get("control_revision"),
            "targets": targets,
            "active_target_id": observed.get("active_target_id"),
        }
