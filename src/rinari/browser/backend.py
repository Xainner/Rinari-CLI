"""Browser backend interface (documento 03 §4).

`BrowserManager` deja de ser el único que sabe hablar con una página. Las
operaciones page-level pasan por un backend, y hay dos:

- `ExternalBackend` — la conexión CDP a un Chromium administrado o a un
  endpoint explícito. Es el camino del CLI y no cambia de comportamiento.
- `HostBackend` — las operaciones las ejecuta el host Electron sobre la
  `WebContentsView` que el usuario está viendo, por el broker de stdio.

Lo que **no** es esta interfaz: un CDP público. El §4.2 pide «interfaz
semántica, no CDP público ilimitado», y la sonda de viabilidad dio la razón por
la que importa: desde una sesión page-level responden `Target.getTargets`,
`Browser.getVersion` y `Browser.setDownloadBehavior`, y la enumeración cruza
particiones. Un passthrough daría a una herramienta ámbito mayor que su target.

Por eso `HostBackend` traduce cada llamada a una operación con nombre de una
allowlist y rechaza lo que no esté en ella, en vez de reenviar el método que le
pidan. La firma se parece a CDP porque es el punto de corte más estrecho de
`manager.py`; la autoridad está en la traducción, no en la forma.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

# Códigos que ya existen en `BrowserError` y describen el mismo problema; el
# §4.2 pide conservarlos donde apliquen y añadir sólo con esquema y tests.
UNSUPPORTED = "BROWSER_UNSUPPORTED"


@runtime_checkable
class BrowserBackend(Protocol):
    """Operaciones que un backend debe saber hacer sobre sus propias páginas."""

    #: `external` | `electron-native`. Viaja en `status()` para que la UI y los
    #: diagnósticos no tengan que adivinar quién está sirviendo.
    kind: str

    @property
    def connected(self) -> bool: ...

    def status(self) -> dict[str, Any]:
        """Estado seguro para enseñar: sin endpoints, cookies ni secretos."""
        ...

    def targets(self) -> list[dict[str, Any]]:
        """Sólo las páginas de **este** contexto.

        Nunca el `Target.getTargets` global: la sonda comprobó que devuelve
        targets de otras particiones (§5.3).
        """
        ...

    def new_page(self, url: str = "about:blank") -> dict[str, Any]: ...

    def close_page(self, target_id: str) -> dict[str, Any]: ...

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
        """Ejecuta una operación page-level sobre un target de este contexto."""
        ...

    def close(self) -> dict[str, Any]:
        """Suelta lo que sea de este backend. Idempotente."""
        ...
