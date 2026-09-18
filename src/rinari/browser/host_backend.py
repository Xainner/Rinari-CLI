"""Backend que ejecuta las operaciones en el host Electron (documento 03 §4, §5).

Las herramientas siguen llamando a `BrowserManager` igual que siempre. Lo que
cambia debajo es quién ejecuta: en vez de una conexión CDP a un Chromium que
Rinari lanzó, las operaciones viajan por el broker de stdio hasta la
`WebContentsView` que el usuario tiene delante.

**Este módulo es la frontera semántica.** `manager.py` habla en métodos CDP
porque es su lenguaje interno; aquí cada uno se traduce a una operación con
nombre de una allowlist, y lo que no esté en ella se rechaza. No se reenvía el
método que pidan.

Que eso importe no es teórico. La sonda de viabilidad
(`docs/architecture/browser-native.md` en Rinari-Agent) midió que desde una
sesión page-level responden `Target.getTargets`, `Browser.getVersion` y
`Browser.setDownloadBehavior`, y que la enumeración cruza particiones. Un
passthrough le daría a una herramienta ámbito mayor que su propio target.
"""

from __future__ import annotations

import contextlib
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

from rinari.browser.backend import UNSUPPORTED
from rinari.engine_protocol.browser_host import (
    BrowserHostBridge,
    HostOperationError,
    HostUnavailable,
)

#: Métodos CDP que `manager.py` emite → operación que el host sabe ejecutar.
#:
#: La clave es lo que pide el manager; el valor es lo que viaja por el broker.
#: El host tiene su propia tabla: una operación desconocida allí también se
#: rechaza, de modo que la lista se cumple en los dos extremos.
_OPERATIONS: dict[str, str] = {
    "Page.navigate": "page.navigate",
    "Page.captureScreenshot": "page.screenshot",
    "Runtime.evaluate": "page.evaluate",
    "Accessibility.getFullAXTree": "page.a11y",
    "DOM.getBoxModel": "page.boxModel",
    "Input.dispatchMouseEvent": "page.mouse",
    "Input.dispatchKeyEvent": "page.key",
}

#: Métodos que el manager emite pero que esta etapa **no** implementa.
#:
#: El §6.3 obliga a que una herramienta que antes funcionaba no desaparezca en
#: silencio del escritorio: se devuelve incompatibilidad explícita, con el
#: nombre de la operación, y la entrega F las completa.
_NOT_YET: dict[str, str] = {
    "DOM.setFileInputFiles": "subir archivos",
    "Network.getCookies": "leer cookies",
    "Network.setCookie": "escribir cookies",
    "Browser.setDownloadBehavior": "descargas",
}

#: `DOM.enable` y compañía son preparación de sesión CDP. El host mantiene sus
#: propios dominios habilitados, así que aquí no significan nada y se aceptan
#: sin viajar: convertirlos en error rompería `manager` sin motivo.
_NO_OP = {"DOM.enable", "Page.enable", "Runtime.enable", "Network.enable", "Accessibility.enable"}

#: Operaciones que **cambian** la página. Mientras el usuario tiene el control,
#: el agente no las ejecuta (§7).
#:
#: `page.evaluate` está aquí a propósito. El §4.2 avisa: «no afirmar que
#: `Runtime.evaluate` sea una operación de lectura por su nombre: puede
#: producir efectos». Clasificarla como observación por parecer una lectura
#: sería la forma cómoda de saltarse el arbitraje.
_MUTATING = {
    "page.navigate",
    "page.mouse",
    "page.key",
    "page.evaluate",
    "context.newPage",
    "context.closePage",
    # Cambiar de pestaña no muta el DOM, pero sí cambia **qué página** recibe
    # la siguiente operación. Con el usuario al mando eso es intervenir.
    "context.selectTarget",
}


class HostBackend:
    """Operaciones page-level ejecutadas por el host sobre la vista visible."""

    kind = "electron-native"

    def __init__(
        self,
        bridge: BrowserHostBridge,
        *,
        session_id: str,
        context_id: str,
    ) -> None:
        self._bridge = bridge
        self._session_id = session_id
        self._context_id = context_id
        self._closed = False
        #: Quién manda y en qué revisión (§7). El agente arranca con el control;
        #: la revisión sube en cada transición para que una operación admitida
        #: bajo el control anterior no se cuele después.
        self._control = "agent"
        self._control_revision = 1
        #: Estado de la transición, distinto del propietario. `agent` y `user`
        #: son estables; `taking-user-control` es el hueco entre pedirlo y
        #: confirmarlo, y `uncertain` es no haber podido garantizar exclusión.
        self._control_state = "agent"
        #: Mutaciones admitidas y todavía sin terminar. Tomar el control espera
        #: a que se vacíe: un booleano no impide que una operación que ya pasó
        #: el guard se aplique después de confirmar al usuario.
        self._leases: dict[str, str] = {}
        self._control_lock = threading.RLock()
        self._drained = threading.Condition(self._control_lock)
        self._settled = threading.Event()
        self._settled.set()
        self._on_control_change: Callable[[str], None] | None = None

    # -- estado ------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """Dispuesto: no vuelve. Distinto de que el host esté caído."""
        return self._closed

    @property
    def connected(self) -> bool:
        return not self._closed and self._bridge.available

    def status(self) -> dict[str, Any]:
        """Metadata segura: ni endpoints, ni cookies, ni ids internos del host."""
        return {
            "backend": self.kind,
            "state": "connected" if self.connected else "disconnected",
            "context_id": self._context_id,
            "session_id": self._session_id,
            "control": self._control,
            "control_state": self._control_state,
            "control_revision": self._control_revision,
        }

    @property
    def control(self) -> str:
        return self._control

    @property
    def control_revision(self) -> int:
        return self._control_revision

    def on_control_change(self, listener: Callable[[str], None]) -> None:
        """Avisa de cada transición ya resuelta. Main monta la barrera aquí."""
        self._on_control_change = listener

    def wait_for_control(self, timeout: float = 10.0) -> str:
        """Espera a que la transición en curso se resuelva. **No** para el loop.

        La usan los tests y los workers; `browser.control.set` no la llama,
        porque se despacha en el loop de stdio y ahí no se puede esperar nada
        (§5.4).
        """
        self._settled.wait(timeout)
        return self._control_state

    def set_control(
        self,
        owner: str,
        *,
        expected_revision: int | None = None,
        drain_timeout_s: float = 10.0,
    ) -> dict[str, Any]:
        """Pide una transición de control (§7). **Vuelve enseguida.**

        Tomar el control cierra la admisión de mutaciones de inmediato y
        después espera —en un worker— a que termine lo que ya se había
        admitido. Sólo entonces se confirma `user`. Esa espera no puede ocurrir
        aquí: `browser.control.set` se despacha en el loop de stdio del Engine
        y ahí una espera es un bloqueo permanente (§5.4).

        Si lo admitido no termina a tiempo, el estado queda `uncertain` y el
        control **no** cambia: el §7 prohíbe conceder dos controladores sobre
        la misma página.
        """
        if owner not in {"agent", "user"}:
            raise _invalid(f"unknown control owner: {owner}")
        # `True` es `int` en Python y colaba como revisión 1.
        if expected_revision is not None:
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise _invalid("expected_revision must be an integer")
            if expected_revision < 0:
                raise _invalid("expected_revision must not be negative")

        with self._control_lock:
            if expected_revision is not None and expected_revision != self._control_revision:
                raise _conflict(
                    f"the browser control moved on: expected revision {expected_revision}, "
                    f"current is {self._control_revision}"
                )
            if owner == self._control and self._control_state == owner:
                return self.status()

            if owner == "agent":
                # Devolver el control es inmediato: mientras mandaba el usuario
                # no se admitió ninguna mutación del agente, así que no hay
                # nada que drenar.
                self._settle("agent", "agent")
                return self.status()

            # Tomar el control: la admisión se cierra **ya**, no al confirmar.
            # El hueco entre pedirlo y concederlo es justo donde se colaba otra
            # mutación.
            self._control_state = "taking-user-control"
            self._settled.clear()
            outstanding = len(self._leases)

        threading.Thread(
            target=self._drain_then_grant,
            args=(drain_timeout_s,),
            name="browser-control-handoff",
            daemon=True,
        ).start()
        return {**self.status(), "outstanding_mutations": outstanding}

    def _drain_then_grant(self, timeout_s: float) -> None:
        """Espera a las mutaciones admitidas y confirma —o no— la transición."""
        deadline = time.monotonic() + timeout_s
        with self._drained:
            while self._leases:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._drained.wait(remaining)
            if self._control_state != "taking-user-control":
                # Alguien resolvió la transición mientras tanto.
                self._settled.set()
                return
            if self._leases:
                # No se puede garantizar exclusión. Se informa y el control se
                # queda donde estaba; conceder aquí serían dos controladores.
                self._control_state = "uncertain"
                listener, state = self._on_control_change, "uncertain"
            else:
                self._settle("user", "user")
                listener, state = self._on_control_change, "user"
            self._settled.set()
        if listener is not None:
            listener(state)

    def _settle(self, owner: str, state: str) -> None:
        """Fija propietario y estado, subiendo la revisión si cambió el dueño."""
        changed = owner != self._control
        self._control = owner
        self._control_state = state
        if changed:
            self._control_revision += 1
        self._settled.set()
        if changed and self._on_control_change is not None:
            self._on_control_change(state)

    def _admit(self, operation: str) -> str | None:
        """Admite una mutación y devuelve su lease, o la rechaza.

        Observar no toma lease: el §7 deja al agente leer mientras el usuario
        controla, y una lectura larga no debe retrasar el traspaso.
        """
        if operation not in _MUTATING:
            return None
        from rinari.browser.manager import BrowserError

        with self._control_lock:
            if self._control_state != "agent":
                raise BrowserError(
                    "BROWSER_INTERVENED",
                    f"the user is taking control of this browser; {operation} is not "
                    "available until control returns to the agent",
                )
            lease = secrets.token_hex(8)
            self._leases[lease] = operation
            return lease

    def _release(self, lease: str | None) -> None:
        if lease is None:
            return
        with self._drained:
            self._leases.pop(lease, None)
            self._drained.notify_all()

    # -- targets -----------------------------------------------------------

    def targets(self) -> list[dict[str, Any]]:
        """Sólo las páginas registradas en este contexto (§5.3)."""
        result = self._request("context.targets", {})
        pages = result.get("targets")
        return list(pages) if isinstance(pages, list) else []

    def new_page(self, url: str = "about:blank") -> dict[str, Any]:
        lease = self._admit("context.newPage")
        try:
            return self._request("context.newPage", {"url": url})
        finally:
            self._release(lease)

    def select_target(self, target_id: str) -> dict[str, Any]:
        """Elige la pestaña visible del contexto.

        Una operación sin `target_id` va a la activa, así que esto decide sobre
        qué página se opera después.
        """
        lease = self._admit("context.selectTarget")
        try:
            return self._request("context.selectTarget", {"target_id": target_id})
        finally:
            self._release(lease)

    def close_page(self, target_id: str) -> dict[str, Any]:
        lease = self._admit("context.closePage")
        try:
            return self._request("context.closePage", {}, target_id=target_id)
        finally:
            self._release(lease)

    # -- operaciones -------------------------------------------------------

    def send(
        self,
        target_id: str | None,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        domain: str | None = None,
        timeout_s: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if method in _NO_OP:
            return {}
        if method in _NOT_YET:
            raise _unsupported(
                f"{_NOT_YET[method]} todavía no está implementado en el browser del escritorio; "
                "usa el backend externo para esta operación"
            )
        operation = _OPERATIONS.get(method)
        if operation is None:
            # Fail-closed: un método nuevo en `manager.py` no llega al host por
            # el hecho de existir. Aparece aquí o no viaja.
            raise _unsupported(f"{method} is not an allowed desktop browser operation")
        del domain  # El host mantiene sus propios dominios habilitados.
        lease = self._admit(operation)
        try:
            return self._request(
                operation,
                dict(params or {}),
                target_id=target_id,
                timeout_s=timeout_s,
                cancelled=cancelled,
            )
        finally:
            # El lease se suelta pase lo que pase: si un fallo lo dejara
            # colgado, tomar el control no se confirmaría nunca.
            self._release(lease)

    def close(self) -> dict[str, Any]:
        """Suelta el contexto en el host. Idempotente y sin levantar errores.

        Cerrar una sesión no debe fallar porque el host ya no esté: si se fue,
        el contexto se fue con él.
        """
        if self._closed:
            return {"closed": True}
        self._closed = True

        # Se avisa al host en segundo plano y se vuelve enseguida. Cerrar una
        # sesión se despacha en el loop de stdio, y ahí una espera al host no
        # se resuelve nunca: su respuesta necesita ese mismo loop (§5.4).
        #
        # Que sea sin esperar no pierde nada: si el host está, dispone el
        # contexto; si no está, no hay nada que disponer.
        def notify_host() -> None:
            with contextlib.suppress(Exception):
                self._request("context.close", {}, timeout_s=5.0)

        threading.Thread(target=notify_host, name="browser-context-close", daemon=True).start()
        return {"closed": True}

    # -- internos ----------------------------------------------------------

    def _request(
        self,
        operation: str,
        params: dict[str, Any],
        *,
        target_id: str | None = None,
        timeout_s: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        from rinari.browser.manager import BrowserError

        # Un contexto dispuesto no revive por recibir una petición atrasada.
        # La comprobación va antes de emitir para que tampoco lo recree el host
        # al resolver la sesión (§5.3).
        if self._closed and operation != "context.close":
            raise BrowserError(
                "BROWSER_DISCONNECTED",
                "this browser context was already disposed; reopening is an explicit action",
            )

        try:
            return self._bridge.request(
                operation,
                params,
                session_id=self._session_id,
                context_id=self._context_id,
                target_id=target_id,
                timeout_s=timeout_s,
                cancelled=cancelled,
            )
        except HostUnavailable as exc:
            # Nunca se emitió: repetirla es seguro.
            raise BrowserError("BROWSER_DISCONNECTED", str(exc)) from exc
        except HostOperationError as exc:
            # La incertidumbre sólo importa si la operación cambia la página.
            # Una lectura que expiró se puede repetir sin consecuencias; un
            # click, no: pudo haberse aplicado (§5.4).
            uncertain = exc.outcome == "outcome_unknown" and operation in _MUTATING
            raise BrowserError(
                exc.code,
                f"{exc.message} — no se sabe si la operación llegó a aplicarse; "
                "vuelve a observar la página antes de actuar"
                if uncertain
                else exc.message,
                retryable=exc.retryable and not uncertain,
                uncertain=uncertain,
            ) from exc


def _unsupported(message: str) -> Exception:
    from rinari.browser.manager import BrowserError

    return BrowserError(UNSUPPORTED, message)


def _invalid(message: str) -> Exception:
    from rinari.browser.manager import BrowserError

    return BrowserError("INVALID_ARGUMENT", message)


def _conflict(message: str) -> Exception:
    from rinari.browser.manager import BrowserError

    return BrowserError("BROWSER_CONTROL_CONFLICT", message)
