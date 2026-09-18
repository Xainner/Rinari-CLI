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

    # -- estado ------------------------------------------------------------

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
        }

    # -- targets -----------------------------------------------------------

    def targets(self) -> list[dict[str, Any]]:
        """Sólo las páginas registradas en este contexto (§5.3)."""
        result = self._request("context.targets", {})
        pages = result.get("targets")
        return list(pages) if isinstance(pages, list) else []

    def new_page(self, url: str = "about:blank") -> dict[str, Any]:
        return self._request("context.newPage", {"url": url})

    def close_page(self, target_id: str) -> dict[str, Any]:
        return self._request("context.closePage", {}, target_id=target_id)

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
        return self._request(
            operation,
            dict(params or {}),
            target_id=target_id,
            timeout_s=timeout_s,
            cancelled=cancelled,
        )

    def close(self) -> dict[str, Any]:
        """Suelta el contexto en el host. Idempotente y sin levantar errores.

        Cerrar una sesión no debe fallar porque el host ya no esté: si se fue,
        el contexto se fue con él.
        """
        if self._closed:
            return {"closed": True}
        self._closed = True
        with contextlib.suppress(Exception):
            self._request("context.close", {}, timeout_s=5.0)
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
            raise BrowserError("BROWSER_DISCONNECTED", str(exc)) from exc
        except HostOperationError as exc:
            raise BrowserError(exc.code, exc.message, retryable=exc.retryable) from exc


def _unsupported(message: str) -> Exception:
    from rinari.browser.manager import BrowserError

    return BrowserError(UNSUPPORTED, message)
