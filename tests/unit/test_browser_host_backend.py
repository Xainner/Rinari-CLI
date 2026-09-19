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

import threading
from pathlib import Path

import pytest

from rinari.browser import host_backend
from rinari.browser.host_backend import HostBackend
from rinari.browser.manager import BrowserError
from rinari.engine_protocol.browser_host import BrowserHostBridge


class FakeBridge:
    """Bridge que apunta lo que se le pide en vez de hablar con un host."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.calls: list[tuple[str, dict]] = []
        self.answer: dict = {}
        #: Qué levantar en vez de contestar, para probar el camino de fallo.
        self.raises: BaseException | None = None

    def capabilities(self) -> set[str]:
        return {"browser_native_view_v1"}

    def request(self, operation, params, **kw):
        self.calls.append((operation, params))
        if self.raises is not None:
            raise self.raises
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
            # Semánticas: el nombre CDP es vocabulario del manager, no lo que
            # se ejecuta. `DOM.setFileInputFiles` entrega un fichero del disco
            # a la página y `Browser.*` cruza particiones, así que ninguno de
            # los dos viaja tal cual.
            ("DOM.setFileInputFiles", "page.setFileInput"),
            ("Browser.setDownloadBehavior", "context.beginDownload"),
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

    def test_las_operaciones_aun_no_portadas_lo_dicen_con_su_nombre(self, monkeypatch) -> None:
        # El §6.3: una herramienta que antes funcionaba no desaparece en
        # silencio del escritorio.
        #
        # `_NOT_YET` está vacío ahora que subir y descargar ya están portadas,
        # así que lo que se comprueba es el **mecanismo**: sin esto, la próxima
        # operación sin portar caería en «operación no permitida», que no dice
        # cuál es ni que vaya a llegar. Se usa un método inventado para no
        # volver a atar la prueba a las dos que se acaban de portar.
        monkeypatch.setitem(host_backend._NOT_YET, "Page.printToPDF", "imprimir a PDF")
        host, fake = backend()
        with pytest.raises(BrowserError) as raised:
            host.send("t1", "Page.printToPDF", {})
        assert raised.value.code == "BROWSER_UNSUPPORTED"
        assert "imprimir a PDF" in raised.value.message
        assert "backend" in raised.value.message
        assert fake.calls == []

    def test_subir_y_descargar_ya_no_son_incompatibles(self) -> None:
        # Eran las dos que quedaban en `_NOT_YET`. Ahora viajan, y esto lo fija
        # para que nadie las devuelva ahí sin darse cuenta.
        host, fake = backend()
        host.send("t1", "DOM.setFileInputFiles", {"selector": "#f", "files": ["C:/tmp/a.txt"]})
        host.send(None, "Browser.setDownloadBehavior", {"downloadPath": "C:/tmp/art"})
        assert [operation for operation, _ in fake.calls] == [
            "page.setFileInput",
            "context.beginDownload",
        ]

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


class TestArbitraje:
    """Documento 03 §7: el agente no muta mientras el usuario tiene el control."""

    def test_el_agente_arranca_con_el_control(self) -> None:
        host, _ = backend()
        assert host.control == "agent"
        assert host.status()["control_revision"] == 1

    def test_tomar_el_control_bloquea_las_operaciones_que_cambian_la_pagina(self) -> None:
        host, fake = backend()
        host.set_control("user")
        fake.calls.clear()
        for method in (
            "Page.navigate",
            "Input.dispatchMouseEvent",
            "Input.dispatchKeyEvent",
            # Poner un fichero en un formulario es editarlo, y además se lo
            # entrega a la página; aceptar descargas cambia lo que esa página
            # puede provocar en el disco. Con el usuario al mando, ninguna.
            "DOM.setFileInputFiles",
            "Browser.setDownloadBehavior",
        ):
            with pytest.raises(BrowserError) as raised:
                host.send("t1", method, {})
            assert raised.value.code == "BROWSER_INTERVENED"
            assert raised.value.retryable is False
        # No es que fallen al llegar: es que no salen.
        assert fake.calls == []

    def test_evaluate_cuenta_como_mutacion(self) -> None:
        # El §4.2: «no afirmar que `Runtime.evaluate` sea una operación de
        # lectura por su nombre: puede producir efectos».
        host, fake = backend()
        host.set_control("user")
        fake.calls.clear()
        with pytest.raises(BrowserError) as raised:
            host.send("t1", "Runtime.evaluate", {"expression": "document.title = 'x'"})
        assert raised.value.code == "BROWSER_INTERVENED"
        assert fake.calls == []

    def test_observar_sigue_permitido_con_el_usuario_al_mando(self) -> None:
        # El §7 permite que el agente observe según policy mientras el usuario
        # controla; lo que no puede es mutar a escondidas.
        host, fake = backend()
        host.set_control("user")
        fake.calls.clear()
        host.send("t1", "Page.captureScreenshot", {})
        host.send("t1", "Accessibility.getFullAXTree", {})
        assert [call[0] for call in fake.calls] == ["page.screenshot", "page.a11y"]

    def test_devolver_el_control_vuelve_a_habilitar_la_mutacion(self) -> None:
        host, fake = backend()
        host.set_control("user")
        host.set_control("agent")
        fake.calls.clear()
        host.send("t1", "Page.navigate", {"url": "http://127.0.0.1/x"})
        assert fake.calls == [("page.navigate", {"url": "http://127.0.0.1/x"})]

    def test_cada_transicion_sube_la_revision_sin_esperar_al_host(self) -> None:
        # `browser.control.set` se despacha en el loop de stdio del Engine, que
        # atiende en serie: una espera al host aquí no se resolvería nunca,
        # porque su respuesta necesita ese mismo loop (§5.4). La barrera la
        # monta main al recibir la revisión confirmada, que es el orden del §7.
        host, fake = backend()
        fake.calls.clear()

        host.set_control("user")
        assert host.wait_for_control(timeout=5) == "user"
        assert host.control == "user"
        assert host.control_revision == 2

        host.set_control("agent")
        assert host.wait_for_control(timeout=5) == "agent"
        assert host.control_revision == 3
        assert fake.calls == []

    def test_pedirlo_dos_veces_mientras_se_toma_no_arranca_otra_transicion(self) -> None:
        """Documento 03 §7: el traspaso lo decide **un** hilo.

        Durante `taking-user-control` el propietario sigue siendo el agente,
        así que un segundo click en «tomar control» —o un reintento de la UI—
        no coincidía con la salida temprana y caía abajo: otro worker de drain
        sobre el mismo estado, dos hilos resolviendo la misma transición.
        """
        host, _ = backend()
        lease = host._admit("page.navigate")  # una mutación admitida que no acaba
        assert lease is not None

        primera = host.set_control("user")
        assert primera["control_state"] == "taking-user-control"
        hilos_antes = threading.active_count()

        segunda = host.set_control("user")
        assert segunda["control_state"] == "taking-user-control"
        # Misma transición: ni revisión nueva ni propietario nuevo.
        assert segunda["control_revision"] == primera["control_revision"]
        assert segunda["control"] == primera["control"]
        assert segunda["outstanding_mutations"] == 1
        assert threading.active_count() == hilos_antes

        # Y al soltar la mutación la transición confirma **una** vez.
        host._release(lease)
        assert host.wait_for_control(5.0) == "user"
        assert host.control == "user"

    def test_pedir_el_mismo_control_no_mueve_la_revision(self) -> None:
        host, fake = backend()
        fake.calls.clear()
        assert host.set_control("agent")["control_revision"] == 1
        assert fake.calls == []

    def test_una_revision_desfasada_no_concede_la_transicion(self) -> None:
        # §7: la transición lleva `expected_revision`. Si el control se movió
        # entre leerlo y pedirlo, se rechaza en vez de conceder dos
        # controladores sobre la misma página.
        host, _ = backend()
        host.set_control("user")  # revisión 2
        with pytest.raises(BrowserError) as raised:
            host.set_control("agent", expected_revision=1)
        assert raised.value.code == "BROWSER_CONTROL_CONFLICT"
        assert host.control == "user"

    def test_la_revision_correcta_sí_concede(self) -> None:
        host, _ = backend()
        host.set_control("user")
        assert host.set_control("agent", expected_revision=2)["control"] == "agent"

    def test_un_dueño_desconocido_se_rechaza(self) -> None:
        host, _ = backend()
        with pytest.raises(BrowserError) as raised:
            host.set_control("nadie")
        assert raised.value.code == "INVALID_ARGUMENT"
        assert host.control == "agent"


class TestNadaBloqueaElLoopDeStdio:
    """Documento 03 §5.4, aprendido rompiéndolo.

    El loop de stdio del Engine despacha en serie, y la respuesta del host
    entra por ese mismo loop. Un handler de protocolo que espere al host no se
    queda lento: se queda bloqueado para siempre, y con él todo el canal de
    control —ni Stop, ni cancelación, ni `engine.info`—.

    Lo que se fija aquí es qué operaciones pueden llamarse desde un handler:
    las que no hablan con el host.
    """

    def test_status_no_consulta_al_host(self) -> None:
        # `browser.view.get` lo llama desde el loop.
        host, fake = backend()
        fake.calls.clear()
        host.status()
        assert fake.calls == []

    def test_set_control_no_consulta_al_host(self) -> None:
        # `browser.control.set` se despacha en el loop.
        host, fake = backend()
        fake.calls.clear()
        host.set_control("user")
        host.set_control("agent")
        assert fake.calls == []

    def test_close_vuelve_sin_esperar_al_host(self) -> None:
        # `session.close` y `session.delete` se despachan en el loop.
        import threading
        import time

        entered = threading.Event()
        release = threading.Event()

        class Slow(FakeBridge):
            def request(self, operation, params, **kw):
                entered.set()
                release.wait(5)
                return {}

        host, _ = backend(Slow())
        started = time.time()
        assert host.close() == {"closed": True}
        elapsed = time.time() - started
        release.set()
        # Vuelve enseguida aunque el host tarde: el aviso va en segundo plano.
        assert elapsed < 1.0


class TestLeasesDeControl:
    """Documento 03 §7: tomar el control espera a lo ya admitido.

    La carrera que fijan estas pruebas: una mutación pasa el guard con control
    `agent` → el usuario toma control → se confirma `user` → la operación
    anterior llega al host y muta la página mientras el usuario cree tener
    exclusión. Un booleano de control no la evita; hace falta que la
    transición sepa qué hay admitido y espere.
    """

    def blocking_backend(self):
        """Backend cuyo `request` se queda dentro hasta que se le suelte."""
        entered = threading.Event()
        release = threading.Event()

        class Held(FakeBridge):
            def request(self, operation, params, **kw):
                self.calls.append((operation, params))
                entered.set()
                release.wait(10)
                return {}

        host, fake = backend(Held())
        return host, fake, entered, release

    def test_tomar_el_control_no_se_confirma_con_una_mutacion_en_vuelo(self) -> None:
        host, _, entered, release = self.blocking_backend()

        out: dict = {}

        def mutate() -> None:
            try:
                out["result"] = host.send("t1", "Page.navigate", {"url": "http://127.0.0.1/x"})
            except Exception as exc:  # pragma: no cover - diagnóstico
                out["error"] = exc

        worker = threading.Thread(target=mutate, daemon=True)
        worker.start()
        assert entered.wait(5), "la mutación no llegó a admitirse"

        # El usuario pide control mientras la navegación está dentro.
        pending = host.set_control("user")
        assert pending["control_state"] == "taking-user-control"
        # **No** se concede todavía: el agente sigue teniendo la página.
        assert pending["control"] == "agent"
        assert host.control == "agent"

        release.set()
        worker.join(timeout=5)
        assert host.wait_for_control(timeout=5) == "user"
        assert host.control == "user"

    def test_mientras_se_toma_el_control_no_se_admiten_mutaciones_nuevas(self) -> None:
        host, fake, entered, release = self.blocking_backend()

        worker = threading.Thread(
            target=lambda: host.send("t1", "Page.navigate", {"url": "http://127.0.0.1/x"}),
            daemon=True,
        )
        worker.start()
        assert entered.wait(5)
        host.set_control("user")

        # La admisión se cierra en el mismo instante en que se pide el control,
        # no cuando se confirma: si no, la ventana entre ambos deja pasar otra.
        fake.calls.clear()
        with pytest.raises(BrowserError) as raised:
            host.send("t1", "Input.dispatchMouseEvent", {"type": "mousePressed"})
        assert raised.value.code == "BROWSER_INTERVENED"
        assert fake.calls == []

        release.set()
        worker.join(timeout=5)

    def test_si_lo_admitido_no_termina_no_se_conceden_dos_controladores(self) -> None:
        host, _, entered, release = self.blocking_backend()
        worker = threading.Thread(
            target=lambda: host.send("t1", "Page.navigate", {"url": "http://127.0.0.1/x"}),
            daemon=True,
        )
        worker.start()
        assert entered.wait(5)

        host.set_control("user", drain_timeout_s=0.3)
        state = host.wait_for_control(timeout=5)

        # No se puede afirmar exclusión: la operación sigue viva.
        assert state == "uncertain"
        assert host.control == "agent"
        assert host.status()["control_state"] == "uncertain"

        release.set()
        worker.join(timeout=5)

    def test_una_lectura_no_retiene_la_transicion(self) -> None:
        # El §7 permite observar mientras el usuario controla, así que una
        # lectura no adquiere lease ni puede retrasar el traspaso.
        host, _, entered, release = self.blocking_backend()
        worker = threading.Thread(
            target=lambda: host.send("t1", "Page.captureScreenshot", {}), daemon=True
        )
        worker.start()
        assert entered.wait(5)

        host.set_control("user", drain_timeout_s=2)
        assert host.wait_for_control(timeout=5) == "user"

        release.set()
        worker.join(timeout=5)

    def test_devolver_el_control_invalida_las_referencias_viejas(self) -> None:
        # §7: «devolver el control obliga a refrescar observación antes de usar
        # coordenadas/referencias viejas».
        host, _ = backend()
        seen: list[str] = []
        host.on_control_change(lambda state: seen.append(state))
        host.set_control("user")
        host.wait_for_control(timeout=5)
        host.set_control("agent")
        host.wait_for_control(timeout=5)
        assert host.control == "agent"
        assert "user" in seen and seen[-1] == "agent"

    def test_una_revision_desfasada_se_rechaza_antes_de_cerrar_la_admision(self) -> None:
        host, _ = backend()
        host.set_control("user")
        host.wait_for_control(timeout=5)
        with pytest.raises(BrowserError) as raised:
            host.set_control("agent", expected_revision=1)
        assert raised.value.code == "BROWSER_CONTROL_CONFLICT"
        assert host.control == "user"

    @pytest.mark.parametrize("bad", [True, False, -1, 1.5, "2", None])
    def test_la_revision_tiene_que_ser_un_entero_no_negativo(self, bad) -> None:
        # `True` es `int` en Python y colaba como revisión 1.
        host, _ = backend()
        if bad is None:
            return
        with pytest.raises(BrowserError) as raised:
            host.set_control("user", expected_revision=bad)
        assert raised.value.code in {"INVALID_ARGUMENT", "BROWSER_CONTROL_CONFLICT"}


class TestObservacion:
    """Consola y red por el host (documento 03 §6.3, R10-13).

    El backend externo las drena del buffer de su `CdpSession`; con backend
    nativo no hay ninguna, así que las recoge el host. Lo que se fija aquí es
    que sigan siendo **observación**: ni toman lease, ni se bloquean cuando el
    usuario tiene el control.
    """

    def test_consola_y_red_piden_su_operacion_con_el_limite(self) -> None:
        host, fake = backend()
        fake.answer = {"events": [{"method": "Runtime.consoleAPICalled", "params": {}}]}
        assert host.observed_events("t1", "console", limit=25) == [
            {"method": "Runtime.consoleAPICalled", "params": {}}
        ]
        assert fake.calls == [("page.consoleEvents", {"limit": 25})]

        fake.calls.clear()
        host.observed_events("t1", "network", limit=50)
        assert fake.calls == [("page.networkEvents", {"limit": 50})]

    def test_observar_no_se_bloquea_con_el_usuario_al_mando(self) -> None:
        # §7: el agente puede observar mientras el usuario controla; lo que no
        # puede es mutar a escondidas.
        host, fake = backend()
        host.set_control("user")
        host.wait_for_control(timeout=5)
        fake.calls.clear()
        fake.answer = {"events": []}
        host.observed_events("t1", "console")
        host.observed_events("t1", "network")
        assert [call[0] for call in fake.calls] == ["page.consoleEvents", "page.networkEvents"]

    def test_una_respuesta_sin_eventos_no_revienta(self) -> None:
        host, fake = backend()
        for answer in ({}, {"events": None}, {"events": ["no es un dict", 7]}):
            fake.answer = answer
            assert host.observed_events("t1", "console") == []

    def test_las_cookies_son_del_contexto_y_no_devuelven_valores(self) -> None:
        # §6.3: operaciones de la partición correcta. El valor no cruza el
        # broker: la herramienta ya lo redacta, y pasearlo sería mover una
        # credencial sin que nadie la necesite.
        host, fake = backend()
        fake.answer = {"cookies": [{"name": "sid", "domain": "x", "path": "/"}]}
        assert host.cookies("t1")["cookies"][0]["name"] == "sid"
        assert fake.calls == [("context.cookies", {})]
        assert "value" not in fake.answer["cookies"][0]

    def test_escribir_una_cookie_cuenta_como_mutacion(self) -> None:
        host, fake = backend()
        host.set_control("user")
        host.wait_for_control(timeout=5)
        fake.calls.clear()
        with pytest.raises(BrowserError) as raised:
            host.set_cookie("t1", "sid", "secreto")
        assert raised.value.code == "BROWSER_INTERVENED"
        assert fake.calls == []

    def test_un_contexto_cerrado_no_observa(self) -> None:
        host, fake = backend()
        host.close()
        fake.calls.clear()
        with pytest.raises(BrowserError) as raised:
            host.observed_events("t1", "console")
        assert raised.value.code == "BROWSER_DISCONNECTED"
        assert fake.calls == []


class TestIncertidumbre:
    """Documento 03 §5.4: una mutación de entrega incierta no se reintenta.

    Lo que hay que distinguir no es «falló» de «funcionó», sino **lo que nunca
    llegó a empezar** de **lo que pudo aplicarse**. Lo primero se puede repetir
    sin consecuencias; lo segundo, repetido, hace la acción dos veces.
    """

    def test_sin_host_la_operacion_no_empezó_y_se_puede_repetir(self) -> None:
        real = BrowserHostBridge(lambda payload: None, "engine-1")
        host = HostBackend(real, session_id="s1", context_id="c1")
        with pytest.raises(BrowserError) as raised:
            host.send("t1", "Page.navigate", {"url": "http://127.0.0.1/x"})
        assert raised.value.code == "BROWSER_DISCONNECTED"
        assert raised.value.uncertain is False

    def test_un_timeout_tras_emitir_deja_el_resultado_incierto(self) -> None:
        from rinari.engine_protocol.browser_host import HostOperationError

        class TimedOut(FakeBridge):
            def request(self, operation, params, **kw):
                raise HostOperationError(
                    "BROWSER_TIMEOUT", "el host no respondió", outcome="outcome_unknown"
                )

        host, _ = backend(TimedOut())
        with pytest.raises(BrowserError) as raised:
            host.send("t1", "Input.dispatchMouseEvent", {"type": "mousePressed"})
        assert raised.value.uncertain is True
        # No reintentable: el click pudo haberse aplicado.
        assert raised.value.retryable is False

    def test_la_herramienta_no_anuncia_reintentable_una_mutacion_incierta(self) -> None:
        """El mapa de categorías no puede devolver `retryable` a `True`.

        `BROWSER_TIMEOUT` cae en TIMEOUT y `BROWSER_DISCONNECTED` en
        DEPENDENCY_ERROR, que son categorías reintentables. Para una mutación
        que quizá se aplicó, eso es información de seguridad equivocada: el
        contrato del §5.4 pide reobservar, no repetir.
        """
        from rinari.tools.definition import ToolErrorCode
        from rinari.tools.native.browse import _CODE_MAP, _RETRYABLE, _retryable_for

        for code in ("BROWSER_TIMEOUT", "BROWSER_DISCONNECTED"):
            mapped = _CODE_MAP[code]
            assert mapped in _RETRYABLE, "si deja de estarlo, esta prueba pierde sentido"
            incierto = BrowserError(code, "entrega incierta", uncertain=True)
            assert _retryable_for(incierto, mapped) is False
            # Y lo que no es incierto conserva el comportamiento de siempre.
            claro = BrowserError(code, "no empezó")
            assert _retryable_for(claro, mapped) is True

        # Un código no reintentable sigue sin serlo.
        assert (
            _retryable_for(BrowserError("BROWSER_INTERVENED", "x"), ToolErrorCode.CONFLICT) is False
        )


class TestNativeNoCaeAlCaminoExterno:
    """Documento 03 §1: un contexto no cambia de backend.

    `launch()` sólo retornaba temprano si el manager estaba conectado. Con
    backend nativo y el host caído eso es falso, así que seguía hasta
    `find_browser`/`Popen` y habría abierto un Chromium externo para una sesión
    que es nativa — dos autoridades sobre páginas distintas, y el usuario
    mirando la que ya no manda.
    """

    def manager(self, monkeypatch, connected: bool = False):
        from rinari.browser import manager as manager_module

        estalla = lambda *a, **k: pytest.fail("no se debe tocar el camino externo")  # noqa: E731
        monkeypatch.setattr(manager_module, "find_browser", estalla)
        monkeypatch.setattr(manager_module.subprocess, "Popen", estalla)
        monkeypatch.setattr(manager_module.CdpSession, "__init__", estalla)

        fake = FakeBridge(available=connected)
        backend_obj = HostBackend(fake, session_id="s1", context_id="c1")
        return manager_module.BrowserManager(
            session_id="s1", home_root=Path("."), backend=backend_obj
        ), fake

    def test_launch_con_host_caido_no_lanza_un_navegador_externo(self, monkeypatch) -> None:
        mgr, _ = self.manager(monkeypatch, connected=False)
        with pytest.raises(BrowserError) as raised:
            mgr.launch()
        assert raised.value.code == "BROWSER_DISCONNECTED"

    def test_launch_con_host_vivo_devuelve_el_contexto_nativo(self, monkeypatch) -> None:
        mgr, _ = self.manager(monkeypatch, connected=True)
        assert mgr.launch() == "electron-native"

    def test_connect_no_deja_que_una_tool_elija_backend(self, monkeypatch) -> None:
        # §4.1: «un argumento de herramienta no puede elegir `electron-native`,
        # `host_id` ni `webContentsId` para escapar de la sesión». Al revés
        # también: no puede sacar a una sesión nativa hacia un endpoint ajeno.
        mgr, _ = self.manager(monkeypatch, connected=True)
        with pytest.raises(BrowserError) as raised:
            mgr.connect("ws://127.0.0.1:9222/devtools/browser/abc")
        assert raised.value.code == "BROWSER_UNSUPPORTED"

    def test_un_endpoint_en_el_entorno_tampoco_lo_saca(self, monkeypatch) -> None:
        monkeypatch.setenv("RINARI_BROWSER_CDP", "ws://127.0.0.1:9222/devtools/browser/abc")
        mgr, _ = self.manager(monkeypatch, connected=True)
        with pytest.raises(BrowserError):
            mgr.connect()


class TestContextoCerrado:
    def test_un_backend_cerrado_falla_antes_de_emitir(self) -> None:
        host, fake = backend()
        host.close()
        fake.calls.clear()
        with pytest.raises(BrowserError) as raised:
            host.send("t1", "Page.navigate", {"url": "http://127.0.0.1/x"})
        assert raised.value.code == "BROWSER_DISCONNECTED"
        # Una petición atrasada no puede recrear un contexto ya dispuesto.
        assert fake.calls == []

    def test_cerrado_tampoco_enumera_ni_crea_paginas(self) -> None:
        host, fake = backend()
        host.close()
        fake.calls.clear()
        for call in (lambda: host.targets(), lambda: host.new_page("about:blank")):
            with pytest.raises(BrowserError):
                call()
        assert fake.calls == []


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


class TestSubirYDescargarPorElHost:
    """Documento 03 §6.3: las dos que quedaban del grupo R10-13.

    El trabajo difícil —resolver la ruta contra el sandbox de la sesión,
    calcular la procedencia, elegir el directorio de artefactos— ya lo hace la
    capa de herramientas y no cambia. Lo que se fija aquí es el despacho: que
    el manager no caiga al camino CDP, y que lo que viaje sea semántico.
    """

    def manager(self, tmp_path, monkeypatch):
        from rinari.browser import manager as manager_module

        estalla = lambda *a, **k: pytest.fail("no se debe tocar el camino externo")  # noqa: E731
        monkeypatch.setattr(manager_module.CdpSession, "__init__", estalla)
        fake = FakeBridge(available=True)
        backend_obj = HostBackend(fake, session_id="s1", context_id="c1")
        return manager_module.BrowserManager(
            session_id="s1", home_root=tmp_path, backend=backend_obj
        ), fake

    def test_subir_va_en_una_sola_operacion_semantica(self, tmp_path, monkeypatch) -> None:
        # Sin `objectId` por el puente: sería un handle a un nodo de la página
        # con vida propia al otro lado. El host resuelve el elemento él mismo.
        mgr, fake = self.manager(tmp_path, monkeypatch)
        archivo = tmp_path / "carta.txt"
        archivo.write_text("hola", encoding="utf-8")
        out = mgr.set_file_input("t1", "#adjunto", archivo)
        assert [operation for operation, _ in fake.calls] == ["page.setFileInput"]
        _, params = fake.calls[0]
        assert params["selector"] == "#adjunto"
        assert params["files"] == [str(archivo.resolve())]
        assert "objectId" not in params
        assert out["selector"] == "#adjunto"

    def test_descargar_no_reenvia_el_dominio_browser(self, tmp_path, monkeypatch) -> None:
        # `Browser.setDownloadBehavior` cruza particiones; lo que viaja es una
        # operación del contexto, con su directorio y nada más.
        mgr, fake = self.manager(tmp_path, monkeypatch)
        destino = tmp_path / "artefactos"
        assert mgr.begin_download(destino) == destino
        assert fake.calls == [("context.beginDownload", {"downloadPath": str(destino)})]
        assert destino.is_dir()

    def test_un_fichero_a_medio_bajar_no_se_recoge(self, tmp_path, monkeypatch) -> None:
        # El host escribe `.part` y renombra al terminar. Recogerlo antes daría
        # un sha256 de bytes incompletos presentado como la descarga.
        mgr, _ = self.manager(tmp_path, monkeypatch)
        destino = tmp_path / "artefactos"
        mgr.begin_download(destino)
        (destino / "informe.pdf.part").write_bytes(b"a mitad")
        assert mgr.poll_download(timeout_s=0.4) is None

        (destino / "informe.pdf").write_bytes(b"completo")
        ref = mgr.poll_download(timeout_s=2.0)
        assert ref is not None
        assert ref.suggested_name == "informe.pdf"
        assert ref.bytes == len(b"completo")


class TestMutacionSemanticaIndivisible:
    """Documento 03 §7: una herramienta no queda a medias por un traspaso.

    Un click no es una operación: son unas coordenadas y tres eventos de
    ratón. Con un lease por `send()`, entre `mousePressed` y `mouseReleased`
    había cero leases, el traspaso veía el hueco y concedía el control — y el
    `mouseReleased` se encontraba con `BROWSER_INTERVENED`, dejando la página
    con el botón lógicamente pulsado y al agente sin permiso para soltarlo.
    """

    def test_sin_scope_el_traspaso_se_cuela_entre_dos_suboperaciones(self) -> None:
        # Es el bug, escrito para que se vea: sin scope, el hueco existe.
        host, _ = backend()
        host.send("t1", "Input.dispatchMouseEvent", {"type": "mousePressed"})
        # Aquí no hay ningún lease vivo, así que el traspaso confirma.
        host.set_control("user")
        assert host.wait_for_control(5.0) == "user"
        with pytest.raises(BrowserError) as raised:
            host.send("t1", "Input.dispatchMouseEvent", {"type": "mouseReleased"})
        assert raised.value.code == "BROWSER_INTERVENED"

    def test_con_scope_el_traspaso_espera_a_que_la_herramienta_termine(self) -> None:
        host, fake = backend()
        with host.mutation_scope("browser.click"):
            host.send("t1", "Runtime.evaluate", {"expression": "coords"})
            host.send("t1", "Input.dispatchMouseEvent", {"type": "mousePressed"})

            # El usuario pide el control **a mitad**.
            pedido = host.set_control("user")
            assert pedido["control_state"] == "taking-user-control"
            assert pedido["outstanding_mutations"] == 1
            # No se concede: la herramienta sigue.
            assert host.wait_for_control(0.5) == "taking-user-control"
            assert host.control == "agent"

            # Y el resto de la herramienta entra, que es lo que importa: la
            # página no se queda con el botón pulsado.
            host.send("t1", "Input.dispatchMouseEvent", {"type": "mouseReleased"})

        # Al cerrar el scope se confirma.
        assert host.wait_for_control(5.0) == "user"
        assert host.control == "user"
        assert [operation for operation, _ in fake.calls] == [
            "page.evaluate",
            "page.mouse",
            "page.mouse",
        ]

    def test_anidar_no_toma_un_segundo_lease(self) -> None:
        # `select_option` llama a `evaluate`, que también abre scope. Manda el
        # de fuera; si cada nivel tomara el suyo, el de dentro se soltaría
        # antes de tiempo y reabriría el hueco.
        host, _ = backend()
        with host.mutation_scope("browser.select"):
            with host.mutation_scope("browser.evaluate"):
                assert len(host._leases) == 1
            assert len(host._leases) == 1
        assert host._leases == {}

    def test_una_mutacion_que_falla_suelta_su_lease(self) -> None:
        host, fake = backend()
        from rinari.engine_protocol.browser_host import HostOperationError

        fake.raises = HostOperationError("JS_ERROR", "reventó")
        with pytest.raises(BrowserError), host.mutation_scope("browser.click"):
            host.send("t1", "Runtime.evaluate", {"expression": "x"})
        # Si se quedara colgado, tomar el control no se confirmaría nunca.
        assert host._leases == {}

    def test_observar_dentro_de_un_scope_no_lo_amplia(self) -> None:
        host, _ = backend()
        with host.mutation_scope("browser.click"):
            host.send("t1", "Accessibility.getFullAXTree", {})
            assert len(host._leases) == 1
        assert host._leases == {}
