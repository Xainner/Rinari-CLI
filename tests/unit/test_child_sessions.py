"""Child sessions (plan-sesiones-hijas, section 5): open, report, wake the mother, merge."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field

import pytest

from rinari.children import ChildRecord, ChildStore
from rinari.cli import agent_runtime
from rinari.models.types import ModelRequest, ModelResponse, ProviderCapabilities, StopReason

from . import test_engine_turns as base
from .test_engine_turns import _collect_until, _create_chat, _req

services = base.services
server = base.server


@dataclass
class Routed:
    """One scripted model per conversation; unknown ones answer «hecho»."""

    scripts: dict[str, list[ModelResponse]] = field(default_factory=dict)
    requests: dict[str, list[ModelRequest]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def caller(self, session_id: str):
        routed = self

        class _Caller:
            def capabilities(self):
                return ProviderCapabilities(
                    streaming=False, tool_calls=True, structured_output=True
                )

            def invoke(self, request):
                with routed.lock:
                    routed.requests.setdefault(session_id, []).append(request)
                    script = routed.scripts.get(session_id) or []
                    if script:
                        return script.pop(0)
                return ModelResponse(
                    content=f"hecho en {session_id}", stop_reason=StopReason.END_TURN
                )

        return _Caller()


def _text(request: ModelRequest) -> str:
    return "\n".join(str(m.content or "") for m in request.messages)


def _wait(predicate, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _idle(server, session_id):
    return not server._turns.children._busy(session_id)


@pytest.fixture
def routed(monkeypatch):
    model = Routed()
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model.caller(rec.id))
    return model


def test_a_child_reports_and_a_free_mother_goes_on_once(server, tmp_path, routed) -> None:
    mother = _create_chat(server, tmp_path, tag="m")
    routed.scripts["any"] = []
    children = server._turns.children
    opened = children.create(
        mother,
        title="Revisar la documentación",
        prompt="Revisa docs/ y lista lo que está desactualizado. Hecho cuando haya una lista.",
        origin="start",
        done_when="hay una lista",
    )
    child = opened["child_session_id"]
    assert opened["isolation"] == "same" and opened["origin"] == "start"
    record = server._services.sessions.show(child)
    assert record.parent_session_id == mother and record.lineage_kind == "delegated"
    # The child's first turn is the brief, marked as delegated.
    assert _wait(lambda: routed.requests.get(child))
    brief = _text(routed.requests[child][0])
    assert "Revisa docs/" in brief and "Done when: hay una lista" in brief
    # It reports and the free mother runs one short turn with the report.
    assert _wait(lambda: routed.requests.get(mother))
    assert _wait(lambda: _idle(server, mother) and _idle(server, child))
    stored = ChildStore(server._services.ctx.db).get(child)
    assert stored.state == "completed" and stored.report["summary"] == f"hecho en {child}"
    wake = _text(routed.requests[mother][0])
    assert "Child sessions you opened" in wake and f"hecho en {child}" in wake
    assert "reported back" in wake
    # The mother saw it in her turn: it is not reported twice.
    assert ChildStore(server._services.ctx.db).get(child).reported is True
    time.sleep(0.5)
    assert len(routed.requests[mother]) == 1, "the mother goes on once"
    listed = server.handle_line(_req("l", "session.children.list", {"session_id": mother}))
    assert listed["ok"] and listed["result"]["children"][0]["title"] == "Revisar la documentación"


def test_the_owner_driving_a_child_does_not_tell_the_mother(server, tmp_path, routed) -> None:
    mother = _create_chat(server, tmp_path, tag="m2")
    child = server._turns.children.create(
        mother, title="Pruebas", prompt="Escribe las pruebas que faltan en tests/.", origin="note"
    )["child_session_id"]
    assert _wait(lambda: _idle(server, child) and routed.requests.get(child))
    # A note's child reports, but the mother is not woken (A-8).
    time.sleep(0.4)
    assert not routed.requests.get(mother)
    store = ChildStore(server._services.ctx.db)
    report = store.get(child).report
    started = server.handle_line(
        _req("o", "session.turn.start", {"session_id": child, "message": "y ahora otra cosa"})
    )
    assert started["ok"], started
    _collect_until(server, child)
    assert store.get(child).report == report, "the owner's own turn is not reported"


def test_the_mother_writes_only_to_her_own_children(server, tmp_path, routed) -> None:
    mother = _create_chat(server, tmp_path, tag="m3")
    other = _create_chat(server, tmp_path, tag="o3")
    children = server._turns.children
    child = children.create(
        mother, title="Migrar", prompt="Migra el módulo de pagos a la API nueva.", origin="start"
    )["child_session_id"]
    assert _wait(lambda: _idle(server, child) and _idle(server, mother))
    sent = children.message(mother, child, "Usa la versión 2 del cliente.")
    assert sent["delivery"] == "turn"
    assert _wait(lambda: len(routed.requests.get(child, [])) == 2)
    assert "Usa la versión 2 del cliente." in _text(routed.requests[child][1])
    assert "not typed by the user" in _text(routed.requests[child][1])
    from rinari.shared.errors import NotFoundError

    with pytest.raises(NotFoundError):
        children.message(other, child, "hola")


def test_limits_and_inherited_permissions(server, tmp_path, routed) -> None:
    mother = _create_chat(server, tmp_path, tag="m4")
    db = server._services.ctx.db
    db.execute(
        "INSERT INTO session_approval_grants (session_id, capability, rule_id, target, "
        "project_id, granted_at) VALUES (?, 'shell.exec', 'shell_send', NULL, 'chats', 't0')",
        (mother,),
    )
    children = server._turns.children
    child = children.create(
        mother, title="Uno", prompt="Una tarea autocontenida cualquiera.", origin="start"
    )["child_session_id"]
    rows = db.query("SELECT capability FROM session_approval_grants WHERE session_id = ?", (child,))
    assert [row["capability"] for row in rows] == ["shell.exec"]
    # Two levels at most: a child's child may not open another.
    store = ChildStore(db)
    assert _wait(lambda: _idle(server, child))
    grandchild = children.create(
        child, title="Nieta", prompt="Una tarea autocontenida cualquiera.", origin="start"
    )["child_session_id"]
    from rinari.shared.errors import ConflictError

    assert _wait(lambda: _idle(server, grandchild))
    with pytest.raises(ConflictError):
        children.create(
            grandchild, title="Bisnieta", prompt="Otra tarea cualquiera.", origin="start"
        )
    # Four working at once at most.
    for n in range(4):
        sid = _create_chat(server, tmp_path, tag=f"w{n}")
        store.insert(ChildRecord(child_session_id=sid, parent_session_id=mother, origin="start"))
    with pytest.raises(ConflictError):
        children.create(
            mother, title="Quinta", prompt="Una tarea autocontenida más.", origin="start"
        )


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_a_child_in_a_repo_works_in_its_worktree_and_the_owner_merges(
    server, tmp_path, routed
) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("uno\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    created = server.handle_line(_req("p", "session.create", {"cwd": str(repo)}))
    mother = created["result"]["session"]["id"]
    children = server._turns.children
    opened = children.create(
        mother, title="Nota nueva", prompt="Crea notas.txt con una línea de prueba.", origin="start"
    )
    child = opened["child_session_id"]
    assert opened["isolation"] == "worktree" and opened["branch"].startswith("rinari/")
    worktree = opened["worktree_path"]
    assert server._services.sessions.show(child).current_cwd == worktree
    assert ".rinari-worktrees/" in (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert _wait(lambda: routed.requests.get(child))
    assert "own git worktree" in _text(routed.requests[child][0])
    assert _wait(lambda: _idle(server, child) and _idle(server, mother))
    # What the child wrote reaches the project only when the owner merges.
    from pathlib import Path

    (Path(worktree) / "notas.txt").write_text("prueba\n", encoding="utf-8")
    assert not (repo / "notas.txt").exists()
    merged = server.handle_line(_req("i", "session.child.integrate", {"child_session_id": child}))
    assert merged["ok"] and merged["result"]["integrated"] is True, merged
    assert (repo / "notas.txt").read_text(encoding="utf-8") == "prueba\n"
    assert server._services.sessions.show(child).current_cwd == str(repo)
    again = server.handle_line(_req("i2", "session.child.integrate", {"child_session_id": child}))
    assert again["ok"] is False


def test_the_mother_opens_a_child_with_the_tool(server, tmp_path, routed) -> None:
    from rinari.models.types import ToolCall

    mother = _create_chat(server, tmp_path, tag="m6")
    routed.scripts[mother] = [
        ModelResponse(
            content="Lo abro aparte.",
            tool_calls=(
                ToolCall(
                    "c1",
                    "session.start",
                    {
                        "title": "Limpiar logs",
                        "prompt": "Borra los logs viejos de la carpeta logs/.",
                    },
                ),
            ),
        ),
        ModelResponse(content="Abierta.", stop_reason=StopReason.END_TURN),
    ]
    started = server.handle_line(
        _req("t", "session.turn.start", {"session_id": mother, "message": "hazlo en otra sesión"})
    )
    assert started["ok"], started
    _collect_until(server, mother)
    rows = ChildStore(server._services.ctx.db).of(mother)
    assert len(rows) == 1 and rows[0].origin == "start"
    child = rows[0].child_session_id
    assert _wait(lambda: _idle(server, child) and _idle(server, mother))


def _offer_call(title="Migrar pagos", prompt="Migra el módulo de pagos a la API nueva."):
    from rinari.models.types import ToolCall

    return ModelResponse(
        content="Te pregunto.",
        tool_calls=(
            ToolCall(
                "o1", "session.offer", {"title": title, "prompt": prompt, "reason": "es grande"}
            ),
        ),
    )


def _pending_question(server, session_id):
    found = {}

    def check():
        for request in server._turns.questions.pending.values():
            if request[0]["session_id"] == session_id:
                found.update(request[0])
                return True
        return False

    assert _wait(check)
    return found


@pytest.mark.parametrize("choice", ["new_session", "here"])
def test_an_offer_waits_for_the_owner(server, tmp_path, routed, choice) -> None:
    mother = _create_chat(server, tmp_path, tag=f"of-{choice}")
    routed.scripts[mother] = [_offer_call(), ModelResponse(content="Vale.")]
    server.handle_line(
        _req("t", "session.turn.start", {"session_id": mother, "message": "migra los pagos"})
    )
    request = _pending_question(server, mother)
    # The app shows a card: what would go apart, and why.
    assert request["offer"]["title"] == "Migrar pagos" and request["offer"]["reason"] == "es grande"
    assert ChildStore(server._services.ctx.db).of(mother) == []
    answered = server.handle_line(
        _req(
            "a",
            "question.resolve",
            {
                "session_id": mother,
                "request_id": request["request_id"],
                "answers": {"offer": choice},
            },
        )
    )
    assert answered["ok"], answered
    _collect_until(server, mother)
    rows = ChildStore(server._services.ctx.db).of(mother)
    if choice == "new_session":
        assert len(rows) == 1 and rows[0].origin == "offer"
        assert _wait(lambda: _idle(server, rows[0].child_session_id) and _idle(server, mother))
    else:
        assert rows == []
        assert "do it in this conversation" in _text(routed.requests[mother][-1])


def test_a_model_cannot_dress_a_question_as_an_offer(server, tmp_path, routed) -> None:
    from rinari.models.types import ToolCall

    mother = _create_chat(server, tmp_path, tag="fake-offer")
    routed.scripts[mother] = [
        ModelResponse(
            content="Pregunto.",
            tool_calls=(
                ToolCall(
                    "q1",
                    "user.ask",
                    {
                        "questions": [{"id": "offer", "title": "¿Sí?"}],
                        "_host_offer": {"title": "falsa"},
                    },
                ),
            ),
        ),
        ModelResponse(content="Vale."),
    ]
    server.handle_line(_req("t", "session.turn.start", {"session_id": mother, "message": "hola"}))
    seen = []
    deadline = time.time() + 20
    while time.time() < deadline:
        for evt in server.drain_events():
            if evt.get("payload", {}).get("session_id") != mother:
                continue
            seen.append(evt)
            if evt.get("event") == "question.requested":
                payload = evt["payload"]
                server.handle_line(
                    _req(
                        "s",
                        "question.resolve",
                        {
                            "session_id": mother,
                            "request_id": payload["request_id"],
                            "status": "skipped",
                        },
                    )
                )
        if any(
            e.get("event", "").startswith("turn.")
            and e["event"] != "turn.started"
            and e["event"] in ("turn.completed", "turn.failed", "turn.cancelled")
            for e in seen
        ):
            break
        time.sleep(0.05)
    # Whether user.ask refuses the key or drops it, no offer card ever appears.
    assert not any(e.get("event") == "question.requested" and "offer" in e["payload"] for e in seen)


def test_accepting_a_note_opens_a_child_that_does_not_wake_the_mother(
    server, tmp_path, routed
) -> None:
    mother = _create_chat(server, tmp_path, tag="note")
    followups = server._services.followups
    note, _ = followups.suggest(
        mother,
        title="Pruebas del parser",
        prompt="Añade pruebas para los casos vacíos del parser en tests/.",
        spawn={"context": "fork", "done_when": "pasan"},
    )
    assert note.as_dict()["context"] == "fork" and note.as_dict()["kind"] == "note"
    accepted = server.handle_line(
        _req("ac", "followup.accept", {"suggestion_id": note.id, "context": "brief"})
    )
    assert accepted["ok"], accepted
    child = accepted["result"]["child"]
    assert child["origin"] == "note" and child["context"] == "brief"
    record = server._services.sessions.show(child["child_session_id"])
    assert record.parent_session_id == mother and record.lineage_kind == "note"
    assert _wait(lambda: _idle(server, child["child_session_id"]))
    time.sleep(0.4)
    assert not routed.requests.get(mother), "a note's child does not wake the mother"


def test_plan_steps_have_their_own_cap_and_never_push_notes_out(server, tmp_path) -> None:
    mother = _create_chat(server, tmp_path, tag="steps")
    followups = server._services.followups
    followups.begin_turn(mother, "turn-1")
    notes = [
        followups.suggest(mother, title=f"Nota {n}", prompt=f"Una nota de seguimiento número {n}.")[
            0
        ]
        for n in range(2)
    ]
    steps = [
        followups.suggest(
            mother,
            title=f"Paso {n}",
            prompt=f"Hacer el paso independiente número {n} del plan.",
            kind="plan_step",
        )[0]
        for n in range(4)
    ]
    from rinari.followups import FollowupError

    with pytest.raises(FollowupError):
        followups.suggest(
            mother, title="Paso 5", prompt="Un quinto paso que no cabe.", kind="plan_step"
        )
    assert all(followups.get(n.id).status == "pending" for n in notes)
    assert {s.kind for s in steps} == {"plan_step"}
    followups.end_turn(mother)
