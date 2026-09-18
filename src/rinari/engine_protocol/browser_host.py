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
from rinari.engine_protocol.messages import event

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
    """El host devolvió un error con código propio.

    `outcome` dice qué se sabe de la ejecución, que no es lo mismo que el
    código del error:

    - `not_started` — la solicitud no llegó a emitirse. Repetirla es seguro.
    - `outcome_unknown` — se emitió y no se sabe si se aplicó. Repetirla
      podría hacer la acción dos veces (§5.4).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        outcome: str = "not_started",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.outcome = outcome


class _Pending:
    """Una solicitud en vuelo, con la identidad completa del recurso.

    Guardar sólo el binding hacía que el crash de **un** contexto fallara los
    pendientes de todos: un mismo binding sirve a todas las sesiones, así que
    una pestaña rota se llevaba por delante el trabajo de las demás.
    """

    __slots__ = (
        "binding_id",
        "context_id",
        "created_at",
        "done",
        "error",
        "operation",
        "result",
        "session_id",
        "target_id",
    )

    def __init__(
        self,
        binding_id: str,
        *,
        session_id: str,
        context_id: str,
        target_id: str | None,
        operation: str,
    ) -> None:
        self.done = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: HostOperationError | None = None
        self.binding_id = binding_id
        self.session_id = session_id
        self.context_id = context_id
        self.target_id = target_id
        self.operation = operation
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
        #: Última foto conocida de cada contexto, empujada por el host.
        #:
        #: Se cachea porque `browser.context.get` se despacha en el loop de
        #: stdio: preguntarle al host desde ahí sería un bloqueo permanente
        #: (§5.4). El host avisa cuando algo cambia y la consulta es local.
        self._observed: dict[str, dict[str, Any]] = {}

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
            pending = _Pending(
                binding_id,
                session_id=session_id,
                context_id=context_id,
                target_id=target_id,
                operation=operation,
            )
            self._pending[request_id] = pending

        deadline = timeout_s if timeout_s is not None else DEFAULT_TIMEOUT_S
        # Sobre de evento normal. Es efímero por su destinatario —main lo
        # consume y no lo pasa al timeline—, no por llevar una envoltura
        # distinta: un sobre propio no lo reconocería el transporte del host,
        # que sólo clasifica como evento lo que viene con `type: "event"`.
        self._emit(
            event(
                "host.browser.request",
                {
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
                },
            )
        )

        # Espera troceada para poder atender la cancelación sin perder el
        # derecho a la respuesta: el wait no sostiene ningún lock.
        end = time.time() + deadline
        while True:
            if pending.done.wait(timeout=0.1):
                break
            # Cancelar y expirar ocurren **después** de emitir, así que el host
            # puede estar ejecutando la operación ahora mismo. Se retira el
            # pendiente para no esperar más, pero el resultado queda declarado
            # desconocido: cancelar no deshace lo remoto (§5.4).
            if cancelled is not None and cancelled():
                self._drop(request_id)
                raise HostOperationError(
                    "CANCELLED", f"{operation} was cancelled", outcome="outcome_unknown"
                )
            if time.time() >= end:
                self._drop(request_id)
                raise HostOperationError(
                    "BROWSER_TIMEOUT",
                    f"the desktop host did not answer {operation} in {deadline:.0f}s",
                    outcome="outcome_unknown",
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

        Una pérdida de control invalida los pendientes **del recurso
        afectado**, no los de todo el binding: un mismo binding sirve a todas
        las sesiones, y una pestaña que se cae no puede llevarse por delante el
        trabajo de las demás.
        """
        binding_id = params.get("binding_id")
        kind = params.get("kind")
        with self._lock:
            if binding_id != self._binding_id:
                return {"accepted": False, "reason": "binding_mismatch"}
        # Un evento dirigido a otra instancia del Engine no es de esta.
        instance = params.get("engine_instance_id")
        if instance is not None and instance != self._engine_instance_id:
            return {"accepted": False, "reason": "engine_instance_mismatch"}

        context_id = params.get("context_id")

        # Foto de estado que el host empuja al cambiar de pestaña, abrirla o
        # cerrarla. Se guarda saneada: ni ids de Electron, ni bindings, ni
        # endpoints — sólo lo que la toolbar necesita enseñar.
        if kind == "targets" and isinstance(context_id, str) and context_id:
            self._observed[context_id] = {
                "targets": _safe_targets(params.get("targets")),
                "active_target_id": params.get("active_target_id")
                if isinstance(params.get("active_target_id"), str)
                else None,
            }
            return {"accepted": True}

        if kind not in {"detached", "crashed", "context_closed"}:
            return {"accepted": True}

        target_id = params.get("target_id")
        if not isinstance(context_id, str) or not context_id:
            # Sin contexto no se puede acotar el daño. Se rechaza en vez de
            # tratarlo como pérdida global: el §5.4 no autoriza a invalidar
            # trabajo ajeno por un evento mal formado.
            return {"accepted": False, "reason": "missing_context"}

        failed = self._fail_matching(
            str(binding_id),
            context_id=context_id,
            target_id=target_id if isinstance(target_id, str) and target_id else None,
            code="BROWSER_DISCONNECTED",
            message=f"the desktop browser context reported {kind}",
        )
        return {"accepted": True, "invalidated": failed}

    # -- internos ----------------------------------------------------------

    def observed(self, context_id: str) -> dict[str, Any]:
        """Lo último que el host contó de este contexto. Consulta local."""
        with self._lock:
            snapshot = self._observed.get(context_id)
            return dict(snapshot) if snapshot else {"targets": [], "active_target_id": None}

    def forget(self, context_id: str) -> None:
        with self._lock:
            self._observed.pop(context_id, None)

    def _drop(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)

    def _fail_matching(
        self,
        binding_id: str,
        *,
        context_id: str,
        target_id: str | None,
        code: str,
        message: str,
    ) -> int:
        """Falla sólo lo que pertenece al recurso afectado.

        Con `target_id` se acota a esa página; sin él, al contexto entero. Lo
        de otras sesiones sigue esperando su respuesta.
        """

        def affected(pending: _Pending) -> bool:
            if pending.binding_id != binding_id or pending.context_id != context_id:
                return False
            if target_id is None:
                return True
            # Una operación sin target concreto se resolvía contra la página
            # por defecto del contexto, así que también queda afectada.
            return pending.target_id in (None, target_id)

        with self._lock:
            victims = [p for p in self._pending.values() if affected(p)]
            for request_id, pending in list(self._pending.items()):
                if affected(pending):
                    self._pending.pop(request_id, None)
        for pending in victims:
            if not pending.done.is_set():
                pending.error = HostOperationError(code, message, outcome="outcome_unknown")
                pending.done.set()
        return len(victims)

    def _fail_all(self, binding_id: str, code: str, message: str) -> None:
        """Pérdida global: el host se fue o su binding se revocó."""
        with self._lock:
            victims = [
                pending for pending in self._pending.values() if pending.binding_id == binding_id
            ]
            for request_id, pending in list(self._pending.items()):
                if pending.binding_id == binding_id:
                    self._pending.pop(request_id, None)
        for pending in victims:
            if not pending.done.is_set():
                # Estaba emitida: el host pudo haberla ejecutado antes de
                # caerse, así que su resultado es desconocido, no fallido.
                pending.error = HostOperationError(code, message, outcome="outcome_unknown")
                pending.done.set()

    def shutdown(self) -> None:
        with self._lock:
            binding_id = self._binding_id
            self._binding_id = None
        if binding_id is not None:
            self._fail_all(binding_id, "BROWSER_DISCONNECTED", "the engine is shutting down")


#: Profundidad máxima de una respuesta. Una estructura más honda que esto no
#: se mide: se rechaza, porque recorrerla ya sería el ataque.
MAX_REPLY_DEPTH = 32


def _safe_targets(value: Any) -> list[dict[str, Any]]:
    """Sanea la lista de pestañas que llega del host.

    Sólo pasan los campos que la UI enseña. Lo demás se descarta aquí y no
    porque el consumidor se acuerde de ignorarlo: el §5.2 acota esta metadata
    a «segura», sin endpoints, cookies ni ids internos de Electron.
    """
    if not isinstance(value, list):
        return []
    safe: list[dict[str, Any]] = []
    for entry in value[:64]:
        if not isinstance(entry, dict):
            continue
        target_id = entry.get("target_id")
        if not isinstance(target_id, str) or not target_id:
            continue
        safe.append(
            {
                "target_id": target_id,
                "url": str(entry.get("url") or "")[:2048],
                "title": str(entry.get("title") or "")[:512],
                "active": bool(entry.get("active")),
            }
        )
    return safe


def _too_big(result: dict[str, Any]) -> bool:
    """¿Excede la respuesta el presupuesto de bytes que puede viajar?

    Se miden **bytes UTF-8 de todo el árbol**, no caracteres del primer nivel.
    Las dos cosas importan: la forma normal de una respuesta CDP es anidada
    —`{"result": {"value": ...}}`, `{"nodes": [...]}`— y un carácter no ASCII
    ocupa hasta cuatro bytes, así que contar `len()` de una cadena subestima
    lo que de verdad ocupa la línea.

    El recorrido se corta en cuanto pasa el tope, de modo que una respuesta
    enorme no se serializa entera para descubrir que era enorme.
    """
    return _measure(result, MAX_REPLY_DEPTH, 0) > MAX_REPLY_BYTES


def _measure(value: Any, depth: int, total: int) -> int:
    """Bytes acumulados, con salida temprana al superar el presupuesto."""
    if total > MAX_REPLY_BYTES:
        return total
    if depth <= 0:
        # Demasiado honda para medirla: se trata como excedida.
        return MAX_REPLY_BYTES + 1

    if isinstance(value, str):
        return total + len(value.encode("utf-8", "surrogatepass"))
    if isinstance(value, (bytes, bytearray)):
        return total + len(value)
    if isinstance(value, dict):
        for key, item in value.items():
            total = _measure(key, depth - 1, total)
            total = _measure(item, depth - 1, total)
            if total > MAX_REPLY_BYTES:
                return total
        return total
    if isinstance(value, (list, tuple)):
        for item in value:
            total = _measure(item, depth - 1, total)
            if total > MAX_REPLY_BYTES:
                return total
        return total
    # Números, booleanos y null ocupan poco pero no cero: se cuentan por su
    # forma serializada para que una lista larga de ellos también tope.
    return total + len(repr(value))
