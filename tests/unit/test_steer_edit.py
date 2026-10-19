"""A message sent during the turn: attachments, edit and withdraw (plan 0.4.0 #54)."""

from __future__ import annotations

import threading
import time

from rinari.cli import agent_runtime
from rinari.models.types import ModelRequest, ModelResponse
from tests.unit import test_checklist as base
from tests.unit.test_checklist import _call, _done, _Model

services = base.services
server = base.server


class _Gated(_Model):
    """The first call waits for `gate`; the rest answer at once."""

    def __init__(self, scripted) -> None:
        super().__init__(scripted)
        self.gate = threading.Event()
        self.waiting = threading.Event()

    def invoke(self, request: ModelRequest) -> ModelResponse:
        if self.calls == 0:
            self.waiting.set()
            assert self.gate.wait(20)
        return super().invoke(request)


def _session(server, tmp_path) -> str:
    return _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})["result"][
        "session"
    ]["id"]


def _until_done(server, session_id, timeout=30.0) -> list[dict]:
    seen: list[dict] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in server.drain_events():
            if evt.get("payload", {}).get("session_id") != session_id:
                continue
            seen.append(evt)
            if evt.get("event") in {"turn.completed", "turn.failed", "turn.cancelled"}:
                return seen
        time.sleep(0.02)
    raise AssertionError("turn did not end")


def test_unread_messages_can_be_edited_or_withdrawn_and_carry_files(server, tmp_path, monkeypatch):
    session_id = _session(server, tmp_path)
    # Images follow the vision rules of a normal message; a text file shows
    # the same path without a vision route in the fake model.
    picture = tmp_path / "captura.txt"
    picture.write_text("error 42 en la línea 7", encoding="utf-8")
    model = _Gated([_done("Primera parte."), _done("Listo con la captura.")])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model)
    assert _call(server, "session.turn.start", {"session_id": session_id, "message": "empieza"})[
        "ok"
    ]
    assert model.waiting.wait(20)

    response = _call(
        server,
        "session.turn.steer",
        {"session_id": session_id, "message": "mira esto", "attachments": [str(picture)]},
    )
    assert response["ok"], response
    kept = response["result"]
    dropped = _call(
        server, "session.turn.steer", {"session_id": session_id, "message": "olvida esto"}
    )["result"]
    assert kept["delivery"] == "steer" and dropped["delivery"] == "steer"

    edited = _call(
        server,
        "session.turn.steer.update",
        {"session_id": session_id, "steer_id": kept["steer_id"], "message": "mira esta captura"},
    )
    assert edited["ok"] and edited["result"]["status"] == "updated"
    cancelled = _call(
        server,
        "session.turn.steer.cancel",
        {"session_id": session_id, "steer_id": dropped["steer_id"]},
    )
    assert cancelled["ok"] and cancelled["result"]["status"] == "cancelled"

    model.gate.set()
    seen = _until_done(server, session_id)
    names = [e["event"] for e in seen]
    assert "steer.updated" in names and "steer.cancelled" in names

    second = model.requests[1].messages
    steered = [m for m in second if (m.origin or {}).get("steer_id")]
    assert len(steered) == 1
    assert steered[0].display_content == "mira esta captura"
    assert "captura.txt" in (steered[0].content or "")
    assert "error 42 en la línea 7" in (steered[0].content or "")
    assert steered[0].content.endswith("User request:\nmira esta captura")
    assert steered[0].attachments and steered[0].attachments[0]["name"] == "captura.txt"
    assert not any("olvida esto" in (m.content or "") for m in second)

    # Once read, it can no longer change.
    late = _call(
        server,
        "session.turn.steer.update",
        {"session_id": session_id, "steer_id": kept["steer_id"], "message": "tarde"},
    )
    assert not late["ok"] and late["error"]["code"] == "STEER_ALREADY_READ"


class _Stuck(_Model):
    """The first call never answers (a model writing a huge tool call); the rest do."""

    def __init__(self, scripted) -> None:
        super().__init__(scripted)
        self.entered = 0
        self.waiting = threading.Event()
        self.release = threading.Event()

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.entered += 1
        if self.entered == 1:
            self.waiting.set()
            self.release.wait(30)
            raise RuntimeError("left behind")
        return super().invoke(request)


def test_send_now_on_a_waiting_message_cuts_a_step_that_writes_no_text(
    server, tmp_path, monkeypatch
):
    # P7: the owner waited 22 minutes while the model wrote one tool call;
    # the cut only looked at visible text. «Enviar ahora» on the waiting
    # message ends the step whatever the model is doing.
    session_id = _session(server, tmp_path)
    model = _Stuck([_done("Leído: paro y te muestro la imagen.")])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model)
    assert _call(server, "session.turn.start", {"session_id": session_id, "message": "empieza"})[
        "ok"
    ]
    assert model.waiting.wait(20)
    queued = _call(
        server, "session.turn.steer", {"session_id": session_id, "message": "¿y mi imagen?"}
    )["result"]
    assert queued["delivery"] == "steer"

    promoted = _call(
        server,
        "session.turn.steer.promote",
        {"session_id": session_id, "steer_id": queued["steer_id"]},
    )
    assert promoted["ok"], promoted
    assert promoted["result"]["status"] == "promoted"
    assert "answer" in promoted["result"]["interrupted"]
    try:
        seen = _until_done(server, session_id)
    finally:
        model.release.set()
    assert [e["event"] for e in seen][-1] == "turn.completed"
    steered = [m for m in model.requests[0].messages if (m.origin or {}).get("steer_id")]
    assert len(steered) == 1 and steered[0].origin.get("interrupt") is True
    assert steered[0].content.startswith("[The user interrupted your current step")
    updated = [e for e in seen if e["event"] == "steer.updated"]
    assert updated and updated[0]["payload"]["interrupt"] is True

    late = _call(
        server,
        "session.turn.steer.promote",
        {"session_id": session_id, "steer_id": queued["steer_id"]},
    )
    assert not late["ok"] and late["error"]["code"] == "STEER_ALREADY_READ"
