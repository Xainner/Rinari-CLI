"""Frontera semántica del backend nativo (documento 03 §4.2 y §5.3).

El §4.2 pide «interfaz semántica, no CDP público ilimitado». Estas pruebas
fijan que la allowlist se cumple hacia fuera: lo que `manager.py` pida y no
esté traducido **no viaja**, aunque sea un método CDP perfectamente válido.

La razón está medida, no supuesta: la sonda de viabilidad del host comprobó que
desde una sesión page-level responden `Target.getTargets`, `Browser.getVersion`
y `Browser.setDownloadBehavior`, y que la enumeración cruza particiones. Un
passthrough le daría a una herramienta ámbito mayor que su propio target.
"""

from __future__ import annotations

import pytest

from rinari.browser.host_backend import HostBackend
from rinari.browser.manager import BrowserError
from rinari.engine_protocol.browser_host import BrowserHostBridge


class FakeBridge:
    """Bridge que apunta lo que se le pide en vez de hablar con un host."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.calls: list[tuple[str, dict]] = []
        self.answer: dict = {}

    def capabilities(self) -> set[str]:
        return {"browser_native_view_v1"}

    def request(self, operation, params, **kw):
        self.calls.append((operation, params))
        return self.answer


def backend(bridge: object | None = None) -> tuple[HostBackend, FakeBridge]:
    fake = bridge if isinstance(bridge, FakeBridge) else FakeBridge()
    return HostBackend(fake, session_id="s1", context_id="c1"), fake  # type: ignore[arg-type]


class TestAllowlist:
    @pytest.mark.parametrize(
        ("method", "operation"),
        [
            ("Page.navigate", "page.navigate"),
            ("Page.captureScreenshot", "page.screenshot"),
            ("Runtime.evaluate", "page.evaluate"),
            ("Accessibility.getFullAXTree", "page.a11y"),
            ("DOM.getBoxModel", "page.boxModel"),
            ("Input.dispatchMouseEvent", "page.mouse"),
            ("Input.dispatchKeyEvent", "page.key"),
        ],
    )
    def test_los_metodos_traducidos_viajan_como_operacion(self, method, operation) -> None:
        host, fake = backend()
        host.send("t1", method, {"url": "http://127.0.0.1/x"})
        assert fake.calls == [(operation, {"url": "http://127.0.0.1/x"})]

    @pytest.mark.parametrize(
        "method",
        [
            "Target.getTargets",
            "Target.createTarget",
            "Target.closeTarget",
            "Browser.getVersion",
            "Page.crash",
            "Runtime.callFunctionOn",
            "Emulation.setDeviceMetricsOverride",
        ],
    )
    def test_lo_que_no_esta_traducido_no_llega_al_host(self, method) -> None:
        host, fake = backend()
        with pytest.raises(BrowserError) as raised:
            host.send("t1", method, {})
        assert raised.value.code == "BROWSER_UNSUPPORTED"
        # Lo importante no es el error, es que no se envió nada.
        assert fake.calls == []

    def test_un_metodo_nuevo_en_el_manager_no_pasa_por_existir(self) -> None:
        # Fail-closed: si mañana `manager.py` emite otro método CDP, aparece
        # aquí explícitamente o no viaja. No hay denylist que mantener.
        host, fake = backend()
        with pytest.raises(BrowserError):
            host.send("t1", "Page.setInterceptFileChooserDialog", {"enabled": True})
        assert fake.calls == []

    def test_las_operaciones_aun_no_portadas_lo_dicen_con_su_nombre(self) -> None:
        # El §6.3: una herramienta que antes funcionaba no desaparece en
        # silencio del escritorio.
        host, fake = backend()
        for method in ("Network.getCookies", "DOM.setFileInputFiles"):
            with pytest.raises(BrowserError) as raised:
                host.send("t1", method, {})
            assert raised.value.code == "BROWSER_UNSUPPORTED"
            assert "backend" in raised.value.message
        assert fake.calls == []

    def test_habilitar_dominios_no_viaja_pero_tampoco_falla(self) -> None:
        host, fake = backend()
        assert host.send("t1", "DOM.enable", {}) == {}
        assert fake.calls == []


class TestEstado:
    def test_el_estado_no_revela_endpoint_ni_ids_del_host(self) -> None:
        host, _ = backend()
        status = host.status()
        assert status["backend"] == "electron-native"
        assert status["state"] == "connected"
        for leaky in ("endpoint", "webContentsId", "host_instance_id", "binding_id"):
            assert leaky not in status

    def test_sin_host_el_backend_no_se_declara_conectado(self) -> None:
        host, fake = backend()
        fake.available = False
        assert host.connected is False
        assert host.status()["state"] == "disconnected"

    def test_cerrar_es_idempotente_y_no_falla_si_el_host_se_fue(self) -> None:
        class Broken(FakeBridge):
            def request(self, operation, params, **kw):
                raise RuntimeError("el host ya no está")

        host, _ = backend(Broken())
        assert host.close() == {"closed": True}
        assert host.close() == {"closed": True}


def test_los_targets_salen_de_la_registry_del_contexto() -> None:
    # §5.3: «la enumeración solo devuelve las páginas registradas en ese
    # contexto, no `Target.getTargets` global».
    host, fake = backend()
    fake.answer = {"targets": [{"target_id": "t1", "url": "http://127.0.0.1/x", "title": ""}]}
    assert host.targets() == [{"target_id": "t1", "url": "http://127.0.0.1/x", "title": ""}]
    assert fake.calls == [("context.targets", {})]


def test_el_backend_cumple_el_protocolo_declarado() -> None:
    from rinari.browser.backend import BrowserBackend

    host, _ = backend()
    assert isinstance(host, BrowserBackend)


def test_un_error_del_bridge_se_traduce_a_BrowserError() -> None:
    from rinari.engine_protocol.browser_host import HostOperationError

    class Failing(FakeBridge):
        def request(self, operation, params, **kw):
            raise HostOperationError("BROWSER_TIMEOUT", "sin respuesta", retryable=True)

    host, _ = backend(Failing())
    with pytest.raises(BrowserError) as raised:
        host.send("t1", "Page.navigate", {"url": "http://127.0.0.1/x"})
    assert raised.value.code == "BROWSER_TIMEOUT"
    assert raised.value.retryable is True


def test_sin_host_registrado_la_operacion_dice_desconectado() -> None:
    real = BrowserHostBridge(lambda payload: None, "engine-1")
    host = HostBackend(real, session_id="s1", context_id="c1")
    with pytest.raises(BrowserError) as raised:
        host.send("t1", "Page.navigate", {"url": "http://127.0.0.1/x"})
    assert raised.value.code == "BROWSER_DISCONNECTED"
