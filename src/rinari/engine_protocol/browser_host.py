"""Broker Engine ↔ host de escritorio para el browser nativo (documento 03 §5).

El Engine no abre un puerto CDP para que el host lo use: eso haría visibles
targets fuera de la sesión, incluido el renderer de la propia aplicación
(§5.1). Se usa el canal privado de stdio que ya los une.

Forma del intercambio:

    Engine  --event host.browser.request-->  host (main de Electron)
    Engine  <--method host.browser.reply---  host

La solicitud es un **evento efímero**: no es actividad del usuario y no entra
en el timeline. La respuesta es un método normal, con los campos de correlación
replicados.

Tres reglas que este módulo hace cumplir y que son fáciles de perder:

1. Una respuesta vieja no satisface una solicitud nueva (§5.3). La correlación
   se valida entera, no sólo el `request_id`.
2. La espera de un worker no bloquea el loop de stdio (§5.4). No se sostiene
   ningún lock mientras se espera, para que `reply` y Stop puedan entrar.
3. Un timeout **elimina** el pending y una respuesta tardía se descarta. Una
   mutación cuya entrega quedó incierta no se reintenta sola (§5.4).
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError

#: Tope de solicitudes en vuelo. El §5.4 pide reservar capacidad de entrega
#: para cancelación y replies: si las operaciones del browser pudieran llenar
#: la cola, un Stop se quedaría detrás de una captura.
MAX_INFLIGHT = 8

#: Tope de la respuesta de una operación. Las capturas vuelven como resultado
#: acotado; no se transmiten frames continuos por esta cola (§5.4).
MAX_REPLY_BYTES = 8 * 1024 * 1024

DEFAULT_TIMEOUT_S = 30.0


class HostUnavailable(Exception):
    """No hay host registrado, o su binding dejó de ser válido."""


class HostOperationError(Exception):
    """El host devolvió un error con código propio."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class _Pending:
    __slots__ = ("binding_id", "created_at", "done", "error", "result")

    def __init__(self, binding_id: str) -> None:
        self.done = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: HostOperationError | None = None
        self.binding_id = binding_id
        self.created_at = time.time()


class BrowserHostBridge:
    """Estado del binding con el host y correlación de solicitudes."""

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        engine_instance_id: str,
    ) -> None:
        self._emit = emit
        self._engine_instance_id = engine_instance_id
        self._lock = threading.Lock()
        self._binding_id: str | None = None
        self._generation = 0
        self._host_capabilities: set[str] = set()
        self._pending: dict[str, _Pending] = {}

    # -- registro ----------------------------------------------------------

    @property
    def available(self) -> bool:
        with self._lock:
            return self._binding_id is not None

    def capabilities(self) -> set[str]:
        with self._lock:
            return set(self._host_capabilities)

    def register(self, params: dict[str, Any]) -> dict[str, Any]:
        """`host.browser.register`: main anuncia qué sabe hacer.

        Registrar otra vez **revoca** el binding anterior. Un host que se
        reinició no comparte pendientes con el que se fue.
        """
        host_instance_id = params.get("host_instance_id")
        if not isinstance(host_instance_id, str) or not host_instance_id:
            raise EngineProtocolError(INVALID_PARAMS, "host_instance_id is required")
        raw_caps = params.get("capabilities")
        if not isinstance(raw_caps, list) or not all(isinstance(c, str) for c in raw_caps):
            raise EngineProtocolError(INVALID_PARAMS, "capabilities must be a list of strings")

        with self._lock:
            previous = self._binding_id
            self._binding_id = secrets.token_hex(16)
            self._generation += 1
            self._host_capabilities = set(raw_caps)
            binding_id = self._binding_id
            generation = self._generation
        if previous is not None:
            # Fuera del lock: fallar pendientes despierta workers.
            self._fail_all(previous, "BROWSER_DISCONNECTED", "the desktop host re-registered")
        return {
            "binding_id": binding_id,
            "engine_instance_id": self._engine_instance_id,
            "generation": generation,
            "host_instance_id": host_instance_id,
        }

    def unregister(self, params: dict[str, Any]) -> dict[str, Any]:
        """`host.browser.unregister`: revoca el binding e invalida pendientes."""
        binding_id = params.get("binding_id")
        with self._lock:
            if binding_id != self._binding_id:
                # Un unregister de un binding viejo no tira el actual.
                return {"revoked": False}
            revoked = self._binding_id
            self._binding_id = None
            self._host_capabilities = set()
        self._fail_all(revoked, "BROWSER_DISCONNECTED", "the desktop host unregistered")
        return {"revoked": True}

    # -- solicitudes -------------------------------------------------------

    def request(
        self,
        operation: str,
        params: dict[str, Any],
        *,
        session_id: str,
        context_id: str,
        target_id: str | None = None,
        timeout_s: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Pide una operación al host y espera su respuesta.

        Se llama desde el worker de una herramienta, nunca desde el loop de
        stdio: mientras espera aquí, `reply` y Stop tienen que poder entrar.
        """
        with self._lock:
            binding_id = self._binding_id
            generation = self._generation
            if binding_id is None:
                raise HostUnavailable("no desktop browser host is registered")
            if len(self._pending) >= MAX_INFLIGHT:
                raise HostOperationError(
                    "RESOURCE_EXHAUSTED",
                    f"too many browser operations in flight ({MAX_INFLIGHT})",
                    retryable=True,
                )
            request_id = secrets.token_hex(12)
            pending = _Pending(binding_id)
            self._pending[request_id] = pending

        deadline = timeout_s if timeout_s is not None else DEFAULT_TIMEOUT_S
        self._emit(
            {
                "type": "host.browser.request",
                "request_id": request_id,
                "binding_id": binding_id,
                "engine_instance_id": self._engine_instance_id,
                "session_id": session_id,
                "context_id": context_id,
                "generation": generation,
                "operation": operation,
                "target_id": target_id,
                "params": params,
                "timeout_ms": int(deadline * 1000),
            }
        )

        # Espera troceada para poder atender la cancelación sin perder el
        # derecho a la respuesta: el wait no sostiene ningún lock.
        end = time.time() + deadline
        while True:
            if pending.done.wait(timeout=0.1):
                break
            if cancelled is not None and cancelled():
                self._drop(request_id)
                raise HostOperationError("CANCELLED", f"{operation} was cancelled")
            if time.time() >= end:
                self._drop(request_id)
                raise HostOperationError(
                    "BROWSER_TIMEOUT",
                    f"the desktop host did not answer {operation} in {deadline:.0f}s",
                )

        self._drop(request_id)
        if pending.error is not None:
            raise pending.error
        return pending.result or {}

    def reply(self, params: dict[str, Any]) -> dict[str, Any]:
        """`host.browser.reply`: main devuelve resultado o error."""
        request_id = params.get("request_id")
        binding_id = params.get("binding_id")
        if not isinstance(request_id, str) or not isinstance(binding_id, str):
            raise EngineProtocolError(INVALID_PARAMS, "request_id and binding_id are required")
        if params.get("engine_instance_id") != self._engine_instance_id:
            # Una respuesta dirigida a otra instancia del Engine no es de esta.
            return {"accepted": False, "reason": "engine_instance_mismatch"}

        with self._lock:
            pending = self._pending.get(request_id)
            current = self._binding_id
            if pending is None:
                # Timeout ya cumplido, o cancelada: la respuesta tardía se
                # descarta en vez de resucitar la operación (§5.4).
                return {"accepted": False, "reason": "unknown_or_expired_request"}
            if binding_id != pending.binding_id or binding_id != current:
                return {"accepted": False, "reason": "binding_mismatch"}

        error = params.get("error")
        if isinstance(error, dict):
            pending.error = HostOperationError(
                str(error.get("code") or "BROWSER_PROTOCOL"),
                str(error.get("message") or "the desktop host reported an error"),
                retryable=bool(error.get("retryable")),
            )
        else:
            result = params.get("result")
            if not isinstance(result, dict):
                pending.error = HostOperationError(
                    "BROWSER_PROTOCOL", "the host reply carried no result object"
                )
            elif _too_big(result):
                pending.error = HostOperationError(
                    "RESOURCE_EXHAUSTED",
                    f"the host reply exceeds {MAX_REPLY_BYTES} bytes",
                )
            else:
                pending.result = result
        pending.done.set()
        return {"accepted": True}

    def event(self, params: dict[str, Any]) -> dict[str, Any]:
        """`host.browser.event`: navegación, crash, pérdida de control.

        Una pérdida de control invalida los pendientes de ese binding: el §5.4
        prohíbe reintentar mutaciones tras una desconexión, así que lo que
        estuviera en vuelo termina con error en vez de esperar al timeout.
        """
        binding_id = params.get("binding_id")
        kind = params.get("kind")
        with self._lock:
            if binding_id != self._binding_id:
                return {"accepted": False, "reason": "binding_mismatch"}
        if kind in {"detached", "crashed", "context_closed"}:
            self._fail_all(
                str(binding_id),
                "BROWSER_DISCONNECTED",
                f"the desktop browser context reported {kind}",
            )
        return {"accepted": True}

    # -- internos ----------------------------------------------------------

    def _drop(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)

    def _fail_all(self, binding_id: str, code: str, message: str) -> None:
        with self._lock:
            victims = [
                pending for pending in self._pending.values() if pending.binding_id == binding_id
            ]
            for request_id, pending in list(self._pending.items()):
                if pending.binding_id == binding_id:
                    self._pending.pop(request_id, None)
        for pending in victims:
            if not pending.done.is_set():
                pending.error = HostOperationError(code, message)
                pending.done.set()

    def shutdown(self) -> None:
        with self._lock:
            binding_id = self._binding_id
            self._binding_id = None
        if binding_id is not None:
            self._fail_all(binding_id, "BROWSER_DISCONNECTED", "the engine is shutting down")


def _too_big(result: dict[str, Any]) -> bool:
    """Tamaño aproximado sin serializar dos veces la respuesta entera."""
    total = 0
    for value in result.values():
        if isinstance(value, (str, bytes, bytearray)):
            total += len(value)
        if total > MAX_REPLY_BYTES:
            return True
    return False
