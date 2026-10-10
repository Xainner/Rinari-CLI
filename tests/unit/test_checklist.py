"""The live checklist: stays true, settles from the outcome, reaches the client."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.checklist import ChecklistError, ChecklistService
from rinari.cli import agent_runtime
from rinari.engine_protocol.server import EngineServer
from rinari.models.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
    ToolCall,
)
from rinari.shared.errors import CancelledError
from rinari.tools.native.checklist import checklist_tools


def _items(*statuses: str) -> list[dict]:
    return [
        {"id": f"s{i}", "content": f"Paso {i}", "status": status}
        for i, status in enumerate(statuses, 1)
    ]


def _configured(app_ctx, home):
    services = build_services(app_ctx, user_home=home)
    services.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    services.models.add("fake", "fake-model-1", "fake-one")
    services.providers.use("fake")
    return services


@pytest.fixture
def services(app_ctx, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return _configured(app_ctx, home)


@pytest.fixture
def service(services) -> ChecklistService:
    return services.checklist


@pytest.fixture
def session_id(services, tmp_path):
    return services.sessions.start(tmp_path).session.id


# -- service ---------------------------------------------------------------------


def test_validation_rejects_what_cannot_be_shown_truthfully(service, session_id):
    with pytest.raises(ChecklistError):
        service.replace(session_id, "no list")
    with pytest.raises(ChecklistError, match="duplicate"):
        service.replace(session_id, [_items("pending")[0], _items("pending")[0]])
    with pytest.raises(ChecklistError, match="blocked_reason"):
        service.replace(session_id, _items("blocked"))
    with pytest.raises(ChecklistError, match="status"):
        service.replace(session_id, [{"id": "a", "content": "x", "status": "done"}])
    with pytest.raises(ChecklistError, match="at most"):
        service.replace(session_id, _items(*["pending"] * 21))
    _, warnings = service.replace(session_id, _items("in_progress", "in_progress"))
    assert warnings and "one" in warnings[0]


def test_a_turn_settles_only_the_list_it_worked_on(service, session_id):
    service.begin_turn(session_id, "t1")
    checklist, _ = service.replace(session_id, _items("completed", "in_progress"))
    assert checklist.state == "active" and checklist.turn_id == "t1"
    assert service.settle(session_id, "other-turn", "completed") is None
    settled = service.settle(session_id, "t1", "completed")
    assert settled.state == "open"
    assert service.settle(session_id, "t1", "completed") is None  # already settled


@pytest.mark.parametrize("outcome", ["cancelled", "failed", "stopped", "interrupted"])
def test_an_ending_that_was_not_a_completion_is_interrupted(service, session_id, outcome):
    service.begin_turn(session_id, "t1")
    service.replace(session_id, _items("completed", "pending"))
    assert service.settle(session_id, "t1", outcome).state == "interrupted"


def test_everything_done_is_completed_and_the_next_turn_clears_it(service, session_id):
    service.begin_turn(session_id, "t1")
    service.replace(session_id, _items("completed", "completed"))
    # Even a cancelled turn whose steps were all done ends completed.
    assert service.settle(session_id, "t1", "cancelled").state == "completed"
    assert service.render_for_prompt(session_id) is None
    cleared = service.begin_turn(session_id, "t2")
    assert cleared is not None and cleared.state == "cleared"
    assert service.visible(session_id) is None


def test_an_unfinished_list_is_handed_to_the_next_turn(service, session_id):
    service.begin_turn(session_id, "t1")
    service.replace(
        session_id,
        [
            {"id": "a", "content": "Escribir el parser", "status": "completed"},
            {
                "id": "b",
                "content": "Probarlo",
                "status": "blocked",
                "blocked_reason": "falta el CSV",
            },
        ],
    )
    service.settle(session_id, "t1", "cancelled")
    assert service.begin_turn(session_id, "t2") is None  # kept, not cleared
    text = service.render_for_prompt(session_id)
    assert "(interrupted)" in text and "[x] a: Escribir el parser" in text
    assert "(blocked: falta el CSV)" in text


def test_empty_items_clear_and_the_user_can_clear(service, session_id):
    service.replace(session_id, _items("pending"))
    checklist, _ = service.replace(session_id, [])
    assert checklist.state == "cleared" and service.visible(session_id) is None
    service.replace(session_id, _items("pending"))
    assert service.clear(session_id).state == "cleared"


def test_orphaned_turns_leave_their_list_interrupted(service, session_id):
    service.begin_turn(session_id, "dead")
    service.replace(session_id, _items("in_progress"))
    settled = service.settle_orphans(["dead", "unknown"])
    assert [c.state for c in settled] == ["interrupted"]


def test_revisions_grow_and_counts_are_exact(service, session_id):
    first, _ = service.replace(session_id, _items("pending", "completed"))
    second, _ = service.replace(session_id, _items("completed", "completed", "blocked")[:2])
    assert second.revision == first.revision + 1
    assert first.counts() == {
        "pending": 1,
        "in_progress": 0,
        "completed": 1,
        "blocked": 0,
        "total": 2,
    }


# -- tool ------------------------------------------------------------------------


def _tool():
    return next(t for t in checklist_tools() if t.name == "checklist.update")


def test_the_tool_announces_the_list_on_the_turn(service, session_id):
    seen: list[tuple[str, dict]] = []
    ctx = SimpleNamespace(
        session_id=session_id,
        checklist=service,
        activity_sink=lambda name, payload: seen.append((name, payload)),
    )
    result = _tool().handler({"items": _items("in_progress", "pending")}, ctx)
    assert result.ok and result.data["counts"]["total"] == 2
    assert [name for name, _ in seen] == ["checklist.updated"]
    assert seen[0][1]["reason"] == "model"
    assert [i["status"] for i in seen[0][1]["checklist"]["items"]] == ["in_progress", "pending"]


def test_the_tool_reports_bad_lists_and_a_missing_service(service, session_id):
    bad = _tool().handler(
        {"items": _items("blocked")}, SimpleNamespace(session_id=session_id, checklist=service)
    )
    assert not bad.ok and bad.error.code == "INVALID_ARGUMENT"
    none = _tool().handler({"items": []}, SimpleNamespace(session_id=session_id))
    assert not none.ok and none.error.code == "DEPENDENCY_ERROR"


def test_the_tool_prints_in_the_cli(service, session_id):
    printed: list[str] = []
    ctx = SimpleNamespace(
        session_id=session_id,
        checklist=service,
        activity_sink=None,
        output_sink=lambda stream, text: printed.append(text),
    )
    _tool().handler({"items": _items("completed", "pending")}, ctx)
    assert printed == ["[x] Paso 1\n[ ] Paso 2\n"]


def test_subagents_do_not_get_the_checklist():
    from rinari.agents import runtime

    source = runtime._SubagentRunner._build_registry.__code__.co_consts
    assert any(isinstance(c, tuple) and "checklist." in c for c in source)


# -- over the protocol -------------------------------------------------------------


class _Model:
    """Scripted responses; with `block_after`, the call after them waits."""

    def __init__(self, scripted, block_after: int | None = None) -> None:
        self.scripted = list(scripted)
        self.calls = 0
        self.block_after = block_after
        self.abort = threading.Event()
        self.blocking = threading.Event()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.block_after is not None and self.calls > self.block_after:
            self.blocking.set()
            while not self.abort.wait(0.05):
                pass
            raise CancelledError("cancelled")
        return self.scripted.pop(0)


def _update(items, call_id="c1") -> ModelResponse:
    return ModelResponse(
        content="Anoto los pasos.",
        tool_calls=(ToolCall(call_id, "checklist.update", {"items": items}),),
        stop_reason=StopReason.TOOL_CALLS,
    )


def _done(text="listo") -> ModelResponse:
    return ModelResponse(content=text, stop_reason=StopReason.END_TURN)


@pytest.fixture
def server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


def _call(server, method, params, rid=[0]):  # noqa: B006 - request counter
    rid[0] += 1
    response = server.handle_line(
        json.dumps({"id": f"r{rid[0]}", "method": method, "params": params})
    )
    assert response is not None
    return response


def _turn(server, session_id, message, timeout=30.0):
    assert _call(server, "session.turn.start", {"session_id": session_id, "message": message})["ok"]
    seen: list[dict] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in server.drain_events():
            if evt.get("payload", {}).get("session_id") != session_id:
                continue
            seen.append(evt)
            if evt.get("event") in {"turn.completed", "turn.cancelled", "turn.failed"}:
                return seen
        time.sleep(0.02)
    raise AssertionError("turn did not end")


def _checklist_events(seen):
    return [e["payload"] for e in seen if e["event"] == "checklist.updated"]


def test_a_turn_with_steps_left_ends_open_and_the_next_finishes_it(server, tmp_path, monkeypatch):
    session_id = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})["result"][
        "session"
    ]["id"]
    model = _Model([_update(_items("completed", "in_progress")), _done()])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model)
    seen = _turn(server, session_id, "haz dos cosas")
    updates = _checklist_events(seen)
    assert [u["reason"] for u in updates] == ["model", "turn_completed"]
    assert updates[-1]["checklist"]["state"] == "open"
    names = [e["event"] for e in seen]
    assert names.index("checklist.updated") < names.index("turn.completed")
    assert len({u["turn_id"] for u in updates}) == 1
    got = _call(server, "session.checklist.get", {"session_id": session_id})["result"]
    assert got["checklist"]["state"] == "open"

    # The next turn sees the leftover list in its context and finishes it.
    model2 = _Model([_update(_items("completed", "completed"), "c2"), _done()])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model2)
    seen = _turn(server, session_id, "sigue")
    assert _checklist_events(seen)[-1]["checklist"]["state"] == "completed"

    # A third turn rolls the completed list over: the dock goes away.
    model3 = _Model([_done("hola")])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model3)
    seen = _turn(server, session_id, "gracias")
    assert [u["reason"] for u in _checklist_events(seen)] == ["rollover"]
    got = _call(server, "session.checklist.get", {"session_id": session_id})["result"]
    assert got["checklist"] is None


def test_a_cancelled_turn_leaves_the_list_interrupted_before_turn_cancelled(
    server, tmp_path, monkeypatch
):
    session_id = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})["result"][
        "session"
    ]["id"]
    model = _Model([_update(_items("completed", "in_progress", "pending"))], block_after=1)
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model)
    assert _call(server, "session.turn.start", {"session_id": session_id, "message": "x"})["ok"]
    assert model.blocking.wait(20)
    assert _call(server, "session.checklist.clear", {"session_id": session_id})["ok"] is False
    _call(server, "session.turn.cancel", {"session_id": session_id})
    model.abort.set()
    seen: list[dict] = []
    deadline = time.time() + 30
    while time.time() < deadline and not any(e["event"] == "turn.cancelled" for e in seen):
        seen += [
            e for e in server.drain_events() if e.get("payload", {}).get("session_id") == session_id
        ]
        time.sleep(0.02)
    names = [e["event"] for e in seen]
    assert "turn.cancelled" in names
    last = _checklist_events(seen)[-1]
    assert last["reason"] == "turn_cancelled" and last["checklist"]["state"] == "interrupted"
    assert names.index("checklist.updated") < names.index("turn.cancelled")
    cleared = _call(server, "session.checklist.clear", {"session_id": session_id})["result"]
    assert cleared["checklist"]["state"] == "cleared"


def test_a_plain_chat_never_gets_a_checklist(server, tmp_path, monkeypatch):
    session_id = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})["result"][
        "session"
    ]["id"]
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: _Model([_done()]))
    assert _checklist_events(_turn(server, session_id, "hola")) == []
    got = _call(server, "session.checklist.get", {"session_id": session_id})["result"]
    assert got == {"checklist": None}


def test_compaction_keeps_the_checklist_in_the_task_lists(services, service, session_id):
    service.replace(
        session_id,
        [
            {"id": "a", "content": "Parser", "status": "completed"},
            {"id": "b", "content": "Tests", "status": "in_progress"},
            {"id": "c", "content": "Docs", "status": "blocked", "blocked_reason": "sin acceso"},
        ],
    )
    evidence = services.context.build_evidence(session_id, None)
    assert evidence["tasks_completed"] == ["checklist: Parser"]
    assert evidence["tasks_active"] == ["checklist: Tests"]
    assert evidence["tasks_blocked"] == ["checklist: Docs (blocked: sin acceso)"]
    service.clear(session_id)
    assert "tasks_active" not in services.context.build_evidence(session_id, None)
