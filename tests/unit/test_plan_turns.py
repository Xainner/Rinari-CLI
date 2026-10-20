"""Implement or adjust a finished plan without a user bubble (plan 0.4.0 #55)."""

from __future__ import annotations

import time

from rinari.cli import agent_runtime
from rinari.engine_protocol.plan_turns import plan_steps, plan_title
from tests.unit import test_checklist as base
from tests.unit.test_checklist import _call, _done, _Model

services = base.services
server = base.server

PLAN = "\n".join(
    [
        "# Tarjetas regalo: venta en caja",
        "",
        "Vender tarjetas desde la caja y guardar el saldo.",
        "",
        "## Riesgos",
        "1. La caja vieja no tiene red.",
        "",
        "## Pasos",
        "1. Crear el modelo **GiftCard**.",
        "2. Añadir `POST /api/tarjetas`.",
        "   1. Validar el monto.",
        "3. Pantalla de venta.",
        "",
        "## Pruebas",
        "1. Saldo inicial correcto.",
    ]
)


def test_steps_come_from_the_steps_section_and_the_title_from_the_heading():
    assert plan_title(PLAN) == "Tarjetas regalo: venta en caja"
    assert plan_steps(PLAN) == [
        "Crear el modelo GiftCard.",
        "Añadir POST /api/tarjetas.",
        "Pantalla de venta.",
    ]
    # Without a steps section, every top-level numbered item counts.
    assert plan_steps("Plan\n\n1. Uno\n2. Dos\n") == ["Uno", "Dos"]
    assert plan_steps("Sin pasos numerados.") == []


def _session(server, tmp_path):
    return _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})["result"][
        "session"
    ]["id"]


def _run(server, session_id, params, timeout=30.0):
    response = _call(server, "session.turn.start", {"session_id": session_id, **params})
    assert response["ok"], response
    seen: list[dict] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in server.drain_events():
            if evt.get("payload", {}).get("session_id") != session_id:
                continue
            seen.append(evt)
            if evt.get("event") in {"turn.completed", "turn.cancelled", "turn.failed"}:
                return response["result"]["turn_id"], seen
        time.sleep(0.02)
    raise AssertionError("turn did not end")


def _plan_turn(server, session_id, monkeypatch):
    _call(server, "session.mode.set", {"ref": session_id, "mode": "plan"})
    model = _Model([_done(PLAN)])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model)
    turn_id, _ = _run(server, session_id, {"message": "planea las tarjetas regalo"})
    return turn_id


def test_implement_runs_in_build_with_the_plan_steps_and_no_typed_message(
    server, tmp_path, monkeypatch
):
    session_id = _session(server, tmp_path)
    plan_turn = _plan_turn(server, session_id, monkeypatch)
    model = _Model([_done("Implementado.")])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model)
    _, seen = _run(
        server,
        session_id,
        {"origin": {"kind": "user", "source": "plan_implement", "plan_turn_id": plan_turn}},
    )
    started = next(e for e in seen if e["event"] == "turn.started")["payload"]
    assert started["mode"] == "build"
    assert started["origin"] == {
        "kind": "user",
        "source": "plan_implement",
        "plan_turn_id": plan_turn,
        "plan_title": "Tarjetas regalo: venta en caja",
        "plan_steps": 3,
    }
    assert started["message"] == "Tarjetas regalo: venta en caja"
    seeded = next(
        e["payload"]
        for e in seen
        if e["event"] == "checklist.updated" and e["payload"]["reason"] == "plan"
    )
    assert [i["content"] for i in seeded["checklist"]["items"]] == [
        "Crear el modelo GiftCard.",
        "Añadir POST /api/tarjetas.",
        "Pantalla de venta.",
    ]
    asked = next(
        m
        for m in model.requests[0].messages
        if (m.origin or {}).get("source") not in (None, "turn-state")
    )
    assert "Implement the plan" in (asked.content or "")
    assert "## Pasos" in (asked.content or "")


def test_adjust_stays_in_plan_and_carries_the_comment(server, tmp_path, monkeypatch):
    session_id = _session(server, tmp_path)
    plan_turn = _plan_turn(server, session_id, monkeypatch)
    model = _Model([_done(PLAN.replace("Pantalla de venta.", "Venta desde la tabla existente."))])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model)
    origin = {
        "kind": "user",
        "source": "plan_adjust",
        "plan_turn_id": plan_turn,
        "comment": "usa la tabla existente",
    }
    _, seen = _run(server, session_id, {"origin": origin})
    started = next(e for e in seen if e["event"] == "turn.started")["payload"]
    assert started["mode"] == "plan"
    assert started["origin"]["comment"] == "usa la tabla existente"
    assert started["origin"]["plan_turn_id"] == plan_turn
    assert started["message"] == "usa la tabla existente"
    reasons = [e["payload"]["reason"] for e in seen if e["event"] == "checklist.updated"]
    assert "plan" not in reasons
    asked = next(
        m
        for m in model.requests[0].messages
        if (m.origin or {}).get("source") not in (None, "turn-state")
    )
    assert "usa la tabla existente" in (asked.content or "")


def test_only_a_finished_plan_turn_of_the_same_session_is_accepted(server, tmp_path, monkeypatch):
    session_id = _session(server, tmp_path)
    model = _Model([_done("hola")])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model)
    build_turn, _ = _run(server, session_id, {"message": "hola"})
    (tmp_path / "otra").mkdir()
    other = _session(server, tmp_path / "otra")
    plan_turn = _plan_turn(server, other, monkeypatch)

    def start(origin):
        return _call(server, "session.turn.start", {"session_id": session_id, "origin": origin})

    implement = {"kind": "user", "source": "plan_implement"}
    for bad in (
        {**implement, "plan_turn_id": build_turn},  # not a PLAN turn
        {**implement, "plan_turn_id": plan_turn},  # another conversation's plan
        {**implement, "plan_turn_id": "turn_missing"},
        {"kind": "user", "source": "plan_adjust", "plan_turn_id": plan_turn},  # no comment
        {"kind": "peer", "source": "plan_implement", "plan_turn_id": plan_turn},
    ):
        response = start(bad)
        assert not response["ok"] and response["error"]["code"] == "INVALID_PARAMS", bad
