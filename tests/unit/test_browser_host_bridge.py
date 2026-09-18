"""Broker Engine ↔ host del browser nativo (documento 03 §5.3 y §5.4).

Lo que fijan estas pruebas no es que el camino feliz funcione, sino los tres
invariantes que son fáciles de romper sin notarlo: la correlación completa, que
la espera de un worker no cierre el canal de control, y que una operación
caducada no reviva.
"""

from __future__ import annotations

import threading
import time

import pytest

from rinari.engine_protocol.browser_host import (
    MAX_INFLIGHT,
    BrowserHostBridge,
    HostOperationError,
    HostUnavailable,
)
from rinari.engine_protocol.errors import EngineProtocolError


def bridge() -> tuple[BrowserHostBridge, list[dict]]:
    events: list[dict] = []
    return BrowserHostBridge(events.append, "engine-1"), events


def registered() -> tuple[BrowserHostBridge, list[dict], dict]:
    br, events = bridge()
    reg = br.register({"host_instance_id": "host-1", "capabilities": ["browser_native_view_v1"]})
    return br, events, reg


def ask(br: BrowserHostBridge, out: dict, **kw) -> threading.Thread:
    """Lanza la solicitud en un worker, como hace una herramienta."""

    def run() -> None:
        try:
            out["result"] = br.request(
                kw.pop("operation", "page.navigate"),
                kw.pop("params", {"url": "http://127.0.0.1/fixture"}),
                session_id="s1",
                context_id="c1",
                **kw,
            )
        except Exception as exc:  # se inspecciona en la prueba
            out["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def emitted(events: list[dict]) -> dict:
    """La carga de la solicitud, sacada del sobre de evento normal.

    Va en el mismo sobre que cualquier otro evento a propósito: el transporte
    del host sólo clasifica como evento lo que llega con `type: "event"`, así
    que una envoltura propia se habría quedado sin entregar. Lo efímero es a
    quién va dirigida, no su forma.
    """
    for _ in range(50):
        for entry in events:
            if entry.get("type") == "event" and entry.get("event") == "host.browser.request":
                return entry["payload"]
        time.sleep(0.02)
    raise AssertionError("no se emitió host.browser.request")


class TestRegistro:
    def test_sin_host_registrado_no_hay_operaciones(self) -> None:
        br, _ = bridge()
        assert br.available is False
        with pytest.raises(HostUnavailable):
            br.request("page.navigate", {}, session_id="s1", context_id="c1")

    def test_register_exige_identidad_y_capabilities(self) -> None:
        br, _ = bridge()
        with pytest.raises(EngineProtocolError):
            br.register({"capabilities": []})
        with pytest.raises(EngineProtocolError):
            br.register({"host_instance_id": "h", "capabilities": "browser_native_view_v1"})

    def test_registrar_de_nuevo_revoca_el_binding_anterior(self) -> None:
        br, events, first = registered()
        out: dict = {}
        thread = ask(br, out, timeout_s=5)
        emitted(events)

        second = br.register({"host_instance_id": "host-2", "capabilities": []})
        assert second["binding_id"] != first["binding_id"]
        assert second["generation"] == first["generation"] + 1

        # El pendiente del host que se fue no queda esperando al timeout.
        thread.join(timeout=3)
        assert isinstance(out.get("error"), HostOperationError)
        assert out["error"].code == "BROWSER_DISCONNECTED"

    def test_unregister_de_un_binding_viejo_no_tira_el_actual(self) -> None:
        br, _, first = registered()
        br.register({"host_instance_id": "host-2", "capabilities": []})
        assert br.unregister({"binding_id": first["binding_id"]}) == {"revoked": False}
        assert br.available is True


class TestCorrelacion:
    def test_la_solicitud_lleva_los_campos_de_correlacion(self) -> None:
        br, events, reg = registered()
        out: dict = {}
        ask(br, out, timeout_s=5, target_id="t1")
        request = emitted(events)
        assert request["binding_id"] == reg["binding_id"]
        assert request["engine_instance_id"] == "engine-1"
        assert request["generation"] == reg["generation"]
        assert request["session_id"] == "s1"
        assert request["context_id"] == "c1"
        assert request["target_id"] == "t1"
        assert request["operation"] == "page.navigate"

    def test_una_respuesta_con_binding_ajeno_no_satisface_la_solicitud(self) -> None:
        br, events, _ = registered()
        out: dict = {}
        thread = ask(br, out, timeout_s=2)
        request = emitted(events)

        answer = br.reply(
            {
                "request_id": request["request_id"],
                "binding_id": "binding-de-otro-host",
                "engine_instance_id": "engine-1",
                "result": {"ok": True},
            }
        )
        assert answer == {"accepted": False, "reason": "binding_mismatch"}
        thread.join(timeout=4)
        assert isinstance(out.get("error"), HostOperationError)
        assert out["error"].code == "BROWSER_TIMEOUT"

    def test_una_respuesta_para_otra_instancia_del_engine_se_descarta(self) -> None:
        br, events, reg = registered()
        out: dict = {}
        ask(br, out, timeout_s=5)
        request = emitted(events)
        answer = br.reply(
            {
                "request_id": request["request_id"],
                "binding_id": reg["binding_id"],
                "engine_instance_id": "engine-anterior",
                "result": {},
            }
        )
        assert answer["accepted"] is False
        assert answer["reason"] == "engine_instance_mismatch"

    def test_el_error_del_host_llega_con_su_codigo(self) -> None:
        br, events, reg = registered()
        out: dict = {}
        thread = ask(br, out, timeout_s=5)
        request = emitted(events)
        br.reply(
            {
                "request_id": request["request_id"],
                "binding_id": reg["binding_id"],
                "engine_instance_id": "engine-1",
                "error": {"code": "TARGET_NOT_FOUND", "message": "no such page"},
            }
        )
        thread.join(timeout=3)
        assert out["error"].code == "TARGET_NOT_FOUND"


class TestCaducidadYReplay:
    def test_una_respuesta_tardia_no_resucita_la_operacion(self) -> None:
        br, events, reg = registered()
        out: dict = {}
        thread = ask(br, out, timeout_s=0.3)
        request = emitted(events)
        thread.join(timeout=3)
        assert out["error"].code == "BROWSER_TIMEOUT"

        # El host contesta cuando ya nadie espera: se descarta en vez de dar
        # por buena una mutación cuyo resultado ya se declaró incierto.
        answer = br.reply(
            {
                "request_id": request["request_id"],
                "binding_id": reg["binding_id"],
                "engine_instance_id": "engine-1",
                "result": {"clicked": True},
            }
        )
        assert answer == {"accepted": False, "reason": "unknown_or_expired_request"}

    def test_la_cancelacion_libera_al_worker_sin_esperar_al_timeout(self) -> None:
        br, events, _ = registered()
        out: dict = {}
        stop = threading.Event()
        thread = ask(br, out, timeout_s=30, cancelled=stop.is_set)
        emitted(events)
        started = time.time()
        stop.set()
        thread.join(timeout=5)
        assert out["error"].code == "CANCELLED"
        assert time.time() - started < 5

    def test_un_detach_del_host_termina_lo_que_estuviera_en_vuelo(self) -> None:
        br, events, reg = registered()
        out: dict = {}
        thread = ask(br, out, timeout_s=30)
        emitted(events)
        br.event({"binding_id": reg["binding_id"], "kind": "detached"})
        thread.join(timeout=3)
        assert out["error"].code == "BROWSER_DISCONNECTED"

    def test_un_evento_de_otro_binding_no_toca_los_pendientes(self) -> None:
        br, events, _ = registered()
        out: dict = {}
        thread = ask(br, out, timeout_s=1)
        emitted(events)
        assert br.event({"binding_id": "otro", "kind": "crashed"})["accepted"] is False
        thread.join(timeout=4)
        # Terminó por su propio timeout, no por el evento ajeno.
        assert out["error"].code == "BROWSER_TIMEOUT"


class TestCapacidad:
    def test_se_limitan_las_solicitudes_en_vuelo(self) -> None:
        br, events, _ = registered()
        outs = [{} for _ in range(MAX_INFLIGHT)]
        threads = [ask(br, out, timeout_s=10) for out in outs]
        for _ in range(100):
            if len([e for e in events if e.get("event") == "host.browser.request"]) >= MAX_INFLIGHT:
                break
            time.sleep(0.02)

        # La que sobra se rechaza de inmediato: el §5.4 pide reservar capacidad
        # de entrega para cancelación y replies.
        extra: dict = {}
        ask(br, extra, timeout_s=10).join(timeout=3)
        assert isinstance(extra.get("error"), HostOperationError)
        assert extra["error"].code == "RESOURCE_EXHAUSTED"
        assert extra["error"].retryable is True

        br.shutdown()
        for thread in threads:
            thread.join(timeout=3)

    def test_una_respuesta_desmesurada_no_se_acepta(self) -> None:
        br, events, reg = registered()
        out: dict = {}
        thread = ask(br, out, timeout_s=5)
        request = emitted(events)
        br.reply(
            {
                "request_id": request["request_id"],
                "binding_id": reg["binding_id"],
                "engine_instance_id": "engine-1",
                "result": {"data": "x" * (9 * 1024 * 1024)},
            }
        )
        thread.join(timeout=3)
        assert out["error"].code == "RESOURCE_EXHAUSTED"


def test_el_apagado_del_engine_no_deja_workers_colgados() -> None:
    br, events, _ = registered()
    out: dict = {}
    thread = ask(br, out, timeout_s=30)
    emitted(events)
    br.shutdown()
    thread.join(timeout=3)
    assert out["error"].code == "BROWSER_DISCONNECTED"
    assert br.available is False
