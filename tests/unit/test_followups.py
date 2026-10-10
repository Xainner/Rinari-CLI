"""Follow-up notes: useful, never piling up, never starting work until accepted."""

from __future__ import annotations

import json
import time
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.engine_protocol.server import EngineServer
from rinari.followups import FollowupError
from rinari.models.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
    ToolCall,
)
from rinari.tools.native.followup import followup_tools
from rinari.tools.runtime import PEER_ORIGIN_DENIED_TOOLS


@pytest.fixture
def services(app_ctx, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    container = build_services(app_ctx, user_home=home)
    container.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    container.models.add("fake", "fake-model-1", "fake-one")
    container.providers.use("fake")
    return container


@pytest.fixture
def followups(services):
    return services.followups


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    path = tmp_path / name
    (path / ".rinari").mkdir(parents=True)
    (path / ".rinari" / "project.toml").write_text("", encoding="utf-8")
    return path


def _note(service, session_id, title, prompt="Haz esto con cuidado y comprueba el resultado"):
    return service.suggest(session_id, title=title, prompt=prompt)


# -- service -------------------------------------------------------------------------


def test_at_most_two_per_turn(services, followups, tmp_path):
    session = services.sessions.new(tmp_path, forced_chat=True)
    followups.begin_turn(session.id, "t1")
    _note(followups, session.id, "Primera idea")
    _note(followups, session.id, "Segunda idea")
    with pytest.raises(FollowupError) as exc:
        _note(followups, session.id, "Tercera idea")
    assert exc.value.code == "LIMIT"
    followups.begin_turn(session.id, "t2")
    assert _note(followups, session.id, "Tercera idea")[0].turn_id == "t2"


def test_pending_notes_do_not_pile_up(services, followups, tmp_path):
    session = services.sessions.new(tmp_path, forced_chat=True)
    first, _ = _note(followups, session.id, "Idea uno")
    for title in ("Idea dos", "Idea tres"):
        _note(followups, session.id, title)
    _, superseded = _note(followups, session.id, "Idea cuatro")
    assert [s.id for s in superseded] == [first.id]
    assert followups.get(first.id).status == "superseded"
    assert len(followups.list(session_id=session.id)) == 3


def test_a_recent_duplicate_is_refused_even_after_dismissing(services, followups, tmp_path):
    session = services.sessions.new(_repo(tmp_path), mode="build")
    note, _ = _note(followups, session.id, "Añadir pruebas al parser")
    followups.dismiss(note.id)
    other = services.sessions.new(tmp_path / "repo", mode="build")
    assert other.project_id == session.project_id
    with pytest.raises(FollowupError) as exc:
        _note(followups, other.id, "  añadir PRUEBAS al parser!  ")
    assert exc.value.code == "DUPLICATE"


def test_pending_notes_expire(services, followups, tmp_path):
    session = services.sessions.new(tmp_path, forced_chat=True)
    note, _ = _note(followups, session.id, "Una idea vieja")
    services.ctx.db.execute(
        "UPDATE followup_suggestions SET created_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
        (note.id,),
    )
    assert followups.list(session_id=session.id) == []
    assert followups.get(note.id).status == "expired"


def test_bad_notes_are_refused(services, followups, tmp_path):
    session = services.sessions.new(tmp_path, forced_chat=True)
    with pytest.raises(FollowupError):
        followups.suggest(session.id, title="x", prompt="demasiado corto pero no")
    with pytest.raises(FollowupError):
        followups.suggest(session.id, title="Bien", prompt="corto")


# -- tool ------------------------------------------------------------------------------


def _tool():
    return next(t for t in followup_tools() if t.name == "followup.suggest")


def test_the_tool_leaves_a_note_on_the_turn_with_its_provenance(services, followups, tmp_path):
    session = services.sessions.new(tmp_path, forced_chat=True)
    seen: list[tuple[str, dict]] = []
    ctx = SimpleNamespace(
        session_id=session.id,
        followups=followups,
        turn_state=SimpleNamespace(external_content=True, sources=["example.com"]),
        activity_sink=lambda name, payload: seen.append((name, payload)),
    )
    result = _tool().handler(
        {"title": "Revisar la documentación", "prompt": "Actualiza docs/api.md con los cambios"},
        ctx,
    )
    assert result.ok and result.data["status"] == "pending"
    assert [name for name, _ in seen] == ["followup.suggested"]
    note = seen[0][1]["suggestion"]
    assert note["external_content"] is True and note["sources"] == ["example.com"]


def test_other_agents_and_subagents_cannot_leave_notes():
    from rinari.agents import runtime

    assert "followup.suggest" in PEER_ORIGIN_DENIED_TOOLS
    consts = runtime._SubagentRunner._build_registry.__code__.co_consts
    assert any(isinstance(c, tuple) and "followup." in c for c in consts)


# -- protocol --------------------------------------------------------------------------


class _Model:
    def __init__(self, scripted) -> None:
        self.scripted = list(scripted)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        return self.scripted.pop(0)


@pytest.fixture
def server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


_ids = count()


def _call(server, method, params=None):
    line = {"id": f"r{next(_ids)}", "method": method, "params": params or {}}
    response = server.handle_line(json.dumps(line))
    assert response is not None
    return response


def _ok(response):
    assert response["ok"], response
    return response["result"]


def _until_terminal(server, session_id, timeout=30.0):
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


def test_a_note_from_a_turn_is_accepted_into_a_new_conversation(
    server, services, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    _ok(_call(server, "profile_bundle.create", {"id": "trabajo", "name": "Trabajo"}))
    _ok(_call(server, "profile_bundle.activate", {"id": "trabajo"}))
    opened = _ok(_call(server, "project.open", {"path": str(repo)}))
    source_id = opened["session"]["id"]
    suggest = ModelResponse(
        content="Te dejo una nota.",
        tool_calls=(
            ToolCall(
                "n1",
                "followup.suggest",
                {"title": "Añadir pruebas", "prompt": "Escribe pruebas para el parser en tests/"},
            ),
        ),
        stop_reason=StopReason.TOOL_CALLS,
    )
    script = _Model([suggest, ModelResponse(content="listo", stop_reason=StopReason.END_TURN)])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: script)
    assert _call(server, "session.turn.start", {"session_id": source_id, "message": "x"})["ok"]
    seen = _until_terminal(server, source_id)
    notes = [e["payload"]["suggestion"] for e in seen if e["event"] == "followup.suggested"]
    assert [n["title"] for n in notes] == ["Añadir pruebas"]
    listed = _ok(_call(server, "followup.list", {"rinari_profile_id": "active"}))["suggestions"]
    assert [n["id"] for n in listed] == [notes[0]["id"]]

    # Showing it started nothing; accepting starts a new conversation.
    started = _Model([ModelResponse(content="pruebas escritas", stop_reason=StopReason.END_TURN)])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: started)
    accepted = _ok(_call(server, "followup.accept", {"suggestion_id": notes[0]["id"]}))
    new = accepted["session"]
    assert new["id"] != source_id
    assert (new["project_id"], new["rinari_profile_id"]) == (
        opened["project"]["id"],
        "trabajo",
    )
    assert new["title"] == "Añadir pruebas" and new["mode"] == "build"
    assert accepted["turn"]["turn_id"]
    run = _until_terminal(server, new["id"])
    assert any(e["event"] == "turn.completed" for e in run)
    again = _ok(_call(server, "followup.accept", {"suggestion_id": notes[0]["id"]}))
    assert again["already_accepted"] is True and again["session"]["id"] == new["id"]
    assert services.followups.get(notes[0]["id"]).status == "accepted"
    assert _ok(_call(server, "followup.list", {}))["suggestions"] == []


def test_dismissing_resolves_the_note(server, services, tmp_path):
    session = services.sessions.new(tmp_path, forced_chat=True)
    note, _ = _note(services.followups, session.id, "Limpiar el código")
    server.drain_events()
    dismissed = _ok(_call(server, "followup.dismiss", {"suggestion_id": note.id}))
    assert dismissed["suggestion"]["status"] == "dismissed"
    events = [e["event"] for e in server.drain_events()]
    assert "followup.resolved" in events
    refused = _call(server, "followup.accept", {"suggestion_id": note.id})
    assert refused["ok"] is False and refused["error"]["code"] == "CONFLICT"
    missing = _call(server, "followup.dismiss", {"suggestion_id": "nope"})
    assert missing["ok"] is False and missing["error"]["code"] == "NOT_FOUND"
