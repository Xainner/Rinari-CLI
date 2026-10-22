"""Time from now, the local time in every turn, and «Repetir aquí».

A one-time task can be asked for from now (`after`), the model reads the
local time in each turn's note, and an agent task can run in the conversation
it was asked in, as one more message with its history.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.engine_protocol.protocol import CAPABILITIES
from rinari.engine_protocol.server import EngineServer
from rinari.models.types import ModelRequest, ModelResponse, ProviderCapabilities, StopReason
from rinari.schedule.rules import ScheduleError, resolve_after
from rinari.schedule.service import ScheduledTaskError
from rinari.schedule.tools import ScheduleToolHost, schedule_tools
from rinari.shared.errors import CancelledError


def _epoch(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


# -- time from now -------------------------------------------------------------


def test_after_is_the_local_minute_it_means_never_earlier() -> None:
    now = _epoch("2026-10-21T14:32") + 20
    assert resolve_after({"kind": "once", "after": {"minutes": 30}}, now) == {
        "kind": "once",
        "at": "2026-10-21T15:03",
    }
    exact = resolve_after(
        {"kind": "once", "after": {"hours": 1, "days": 1}}, _epoch("2026-10-21T14:32")
    )
    assert exact["at"] == "2026-10-22T15:32"
    # Anything else is left for parse_schedule to judge.
    daily = {"kind": "daily", "time": "08:00"}
    assert resolve_after(daily, now) is daily


@pytest.mark.parametrize(
    "after",
    [{}, {"weeks": 1}, {"minutes": -5}, {"minutes": 0}, {"minutes": True}, {"days": 400}, "30m"],
)
def test_a_wrong_after_is_refused(after) -> None:
    with pytest.raises(ScheduleError):
        resolve_after({"kind": "once", "after": after}, 1_700_000_000.0)


def test_at_and_after_together_are_refused() -> None:
    with pytest.raises(ScheduleError):
        resolve_after({"kind": "once", "at": "2026-10-21T15:00", "after": {"minutes": 5}}, 0.0)


# -- the service and the tool --------------------------------------------------


@pytest.fixture
def services(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    container = build_services(app_ctx, user_home=user_home)
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


def _task(**overrides):
    return {
        "name": "Revisar el precio",
        "kind": "agent",
        "schedule": {"kind": "interval", "minutes": 30},
        "prompt": "Revisa otra vez el precio y dime si cambió.",
        **overrides,
    }


def tmp_home(services):
    return services.ctx.layout.root


def _chat(services) -> str:
    return services.sessions.new(tmp_home(services), forced_chat=True).id


def test_a_relative_schedule_is_stored_as_the_local_time_it_means(services) -> None:
    task = services.schedules.create(_task(schedule={"kind": "once", "after": {"minutes": 30}}))
    assert set(task["schedule"]) == {"kind", "at"}
    due = datetime.fromisoformat(task["schedule"]["at"]).timestamp()
    created = task["created_at"]
    assert task["next_run_at"] == due
    assert created + 1800 <= due < created + 1800 + 120


def test_the_tool_runs_here_and_answers_the_local_time(services) -> None:
    session_id = _chat(services)
    tool = schedule_tools(ScheduleToolHost(service=services.schedules))[0]
    owner = SimpleNamespace(session_id=session_id, origin_kind="user")
    result = tool.handler(
        _task(schedule={"kind": "once", "after": {"minutes": 30}}, in_this_conversation=True),
        owner,
    )
    assert result.ok is True and result.data["status"] == "created"
    [task] = services.schedules.list()
    assert (task["target"], task["session_id"]) == ("this_conversation", session_id)
    expected = datetime.fromtimestamp(task["next_run_at"]).strftime("%Y-%m-%d %H:%M")
    assert result.data["next_run_local"] == expected
    # The model cannot point a task at another conversation.
    tool.handler(_task(target="this_conversation", session_id="ses_other"), owner)
    assert all(item["session_id"] != "ses_other" for item in services.schedules.list())


def test_a_task_here_is_validated(services) -> None:
    session_id = _chat(services)
    here = {"target": "this_conversation", "session_id": session_id}
    for bad in (
        {"target": "elsewhere"},
        {**here, "kind": "reminder"},
        {**here, "session_id": "ses_missing"},
        {**here, "grants": [{"capability": "shell.exec"}]},
    ):
        with pytest.raises(ScheduledTaskError):
            services.schedules.create(_task(**bad))
    # The conversation's own model, mode and permissions decide; the task's
    # are not kept.
    task = services.schedules.create(
        _task(**here, model="fake-one", permission_profile="full-access", skills=["x"])
    )
    assert (task["model"], task["permission_profile"], task["skills"]) == (None, None, [])
    # Back to a fresh session each time: the conversation is let go.
    moved = services.schedules.update(task["id"], {"target": "new_session"})
    assert (moved["target"], moved["session_id"]) == ("new_session", None)


# -- the runner ----------------------------------------------------------------


@dataclass
class FakeModel:
    scripted: list[str]
    requests: list[ModelRequest] = field(default_factory=list)
    gate: threading.Event | None = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.gate is not None:
            for _ in range(600):
                if self.gate.wait(0.05):
                    break
            else:
                raise CancelledError("test gate never opened")
        return ModelResponse(content=self.scripted.pop(0), stop_reason=StopReason.END_TURN)


@pytest.fixture
def server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


_IDS = itertools.count()


def _call(server, method, params=None):
    line = {"id": f"here-{next(_IDS)}", "method": method, "params": params or {}}
    response = server.handle_line(json.dumps(line))
    assert response is not None
    return response


def _ok(response):
    assert response["ok"] is True, response
    return response["result"]


def _wait(until, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if until():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def _run(server, run_id):
    return server._services.ctx.schedule_repo.run(run_id)


def _finished(server, run_id):
    _wait(lambda: _run(server, run_id)["status"] in {"completed", "failed", "cancelled"})
    return _run(server, run_id)


def _due(server, task_id) -> float:
    return server._services.schedules.get(task_id)["next_run_at"] + 1


def _here(server, session_id, **overrides):
    task = _task(target="this_conversation", session_id=session_id, **overrides)
    return _ok(_call(server, "schedule.create", {"task": task}))["task"]


def _messages(request: ModelRequest) -> list:
    return list(request.messages)


def test_a_run_here_is_one_more_message_in_its_conversation(server, monkeypatch) -> None:
    fake = FakeModel(["Está en 2400.", "El precio subió."])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    _ok(_call(server, "session.turn.start", {"session_id": session_id, "message": "¿Precio?"}))
    _wait(lambda: not server.turns.has_active_turn(session_id))
    before = server._services.sessions.show(session_id)
    task = _here(server, session_id)
    [run_id] = server._schedule.tick(_due(server, task["id"]))
    run = _finished(server, run_id)
    assert run["status"] == "completed" and run["summary"] == "El precio subió."
    assert run["session_id"] == session_id
    after = server._services.sessions.show(session_id)
    # The conversation is the owner's: same title, model, mode, no lineage.
    assert (after.title, after.mode, after.lineage_kind) == (before.title, before.mode, None)
    assert server.turns.grants.for_session(session_id) == []
    messages = _messages(fake.requests[1])
    # With its history: the owner's question and the answer are there.
    assert any(m.content == "¿Precio?" for m in messages)
    prompt = next(i for i, m in enumerate(messages) if m.content == task["prompt"])
    assert messages[prompt].origin["kind"] == "schedule"
    assert messages[prompt].origin["task_name"] == "Revisar el precio"
    # Right after it, the local time of the turn.
    assert messages[prompt + 1].origin == {"kind": "harness", "source": "clock"}
    assert messages[prompt + 1].content.startswith("Local time now: ")


def test_every_turn_carries_its_local_time(server, monkeypatch) -> None:
    fake = FakeModel(["uno", "dos"])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    for text in ("hola", "otra vez"):
        _ok(_call(server, "session.turn.start", {"session_id": session_id, "message": text}))
        _wait(lambda: not server.turns.has_active_turn(session_id))
    clocks = [m for m in _messages(fake.requests[-1]) if (m.origin or {}).get("source") == "clock"]
    assert len(clocks) == 2


def test_a_busy_conversation_queues_the_run_and_its_turn_is_not_the_owners(
    server, monkeypatch
) -> None:
    gate = threading.Event()
    fake = FakeModel(["respuesta al dueño", "respuesta de la tarea"], gate=gate)
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    started: list[dict] = []
    real_start = server.turns.start_turn

    def spy(*args, **kwargs):
        started.append(kwargs)
        return real_start(*args, **kwargs)

    monkeypatch.setattr(server.turns, "start_turn", spy)
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    _ok(_call(server, "session.turn.start", {"session_id": session_id, "message": "hola"}))
    task = _here(server, session_id)
    [run_id] = server._schedule.tick(_due(server, task["id"]))
    assert _run(server, run_id)["status"] == "queued"
    [entry] = server.turns.queue_list(session_id)["entries"]
    assert entry["origin"]["kind"] == "schedule" and entry["origin"]["task_name"] == task["name"]
    # The next tick does not pile a second run behind it.
    [skipped] = server._schedule.tick(_due(server, task["id"]))
    assert _run(server, skipped)["reason"] == "previous_run_active"
    gate.set()
    run = _finished(server, run_id)
    # The owner's turn ended first and did not close the run.
    assert run["status"] == "completed" and run["summary"] == "respuesta de la tarea"
    queued_start = next(kw for kw in started if (kw.get("origin") or {}).get("run_id") == run_id)
    assert queued_start["memory_origin"] == "automation"


def test_clearing_the_queue_cancels_the_waiting_run(server, monkeypatch) -> None:
    gate = threading.Event()
    fake = FakeModel(["ok"], gate=gate)
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    _ok(_call(server, "session.turn.start", {"session_id": session_id, "message": "hola"}))
    task = _here(server, session_id)
    [run_id] = server._schedule.tick(_due(server, task["id"]))
    assert server.turns.queue_clear(session_id)["removed"] == 1
    run = _run(server, run_id)
    assert (run["status"], run["reason"]) == ("cancelled", "cancelled_by_owner")
    gate.set()
    _wait(lambda: not server.turns.has_active_turn(session_id))
    # A restart does not bring it back.
    server._schedule._recovered = False
    server._schedule.tick(_due(server, task["id"]) - 3600)
    assert _run(server, run_id)["status"] == "cancelled"


def test_a_run_whose_queue_died_with_the_engine_runs_once(server, monkeypatch) -> None:
    fake = FakeModel(["de vuelta"])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    task = _here(server, session_id)
    repo = server._services.ctx.schedule_repo
    now = server._services.ctx.clock.now()
    repo.save_run(
        "run_waiting",
        task_id=task["id"],
        status="queued",
        trigger="schedule",
        scheduled_for=now,
        session_id=session_id,
    )
    # A new process: nothing is in the queue, the run is still `queued`.
    server._schedule._recovered = False
    assert server._schedule.tick(now) == []
    run = _finished(server, "run_waiting")
    assert run["status"] == "completed" and run["summary"] == "de vuelta"
    assert len(fake.requests) == 1


def test_an_archived_conversation_runs_and_stays_archived(server, monkeypatch) -> None:
    fake = FakeModel(["hecho"])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    task = _here(server, session_id)
    server._services.sessions.archive(session_id)
    [run_id] = server._schedule.tick(_due(server, task["id"]))
    assert _finished(server, run_id)["status"] == "completed"
    assert server._services.sessions.show(session_id).state == "archived"
    # The owner still cannot write there without bringing it back.
    refused = _call(server, "session.turn.start", {"session_id": session_id, "message": "hola"})
    assert refused["ok"] is False and refused["error"]["code"] == "SESSION_CLOSED"


def test_a_conversation_out_of_memory_still_runs(server, monkeypatch) -> None:
    fake = FakeModel(["hecho"])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    task = _here(server, session_id)
    server._services.memory.exclude_conversation(session_id)
    [run_id] = server._schedule.tick(_due(server, task["id"]))
    assert _finished(server, run_id)["status"] == "completed"


def test_a_deleted_conversation_fails_the_run_and_switches_the_task_off(server) -> None:
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    task = _here(server, session_id)
    server._services.sessions.delete(session_id)
    [run_id] = server._schedule.tick(_due(server, task["id"]))
    run = _run(server, run_id)
    assert (run["status"], run["reason"]) == ("failed", "conversation_deleted")
    assert server._services.schedules.get(task["id"])["enabled"] is False
    # Nothing was created in its place.
    assert server._services.ctx.session_repo.get(session_id) is None


def test_a_grant_cannot_join_a_task_that_runs_here(server, monkeypatch) -> None:
    fake = FakeModel(["hecho"])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    task = _here(server, session_id)
    [run_id] = server._schedule.tick(_due(server, task["id"]))
    _finished(server, run_id)
    refused = _call(
        server, "schedule.grant", {"session_id": session_id, "capability": "shell.exec"}
    )
    assert refused["ok"] is False and refused["error"]["code"] == "INVALID_PARAMS"


def test_a_turn_ending_while_the_run_is_queued_still_starts_it(server, monkeypatch) -> None:
    """The run is `queued` before it is in the queue, and the queue is run again:
    a turn that ends in between can neither strand it nor run it untracked."""
    fake = FakeModel(["respuesta de la tarea"])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    task = _here(server, session_id)
    # Busy when the run looks, but the owner's turn ends before it is queued.
    monkeypatch.setattr(server.turns, "has_active_turn", lambda sid: True)
    [run_id] = server._schedule.tick(_due(server, task["id"]))
    run = _finished(server, run_id)
    assert run["status"] == "completed" and run["summary"] == "respuesta de la tarea"
    assert server.turns.queue_list(session_id)["pending"] == 0


def test_skipped_ticks_never_hide_a_waiting_run(server, monkeypatch) -> None:
    gate = threading.Event()
    fake = FakeModel(["al dueño", "de la tarea"], gate=gate)
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    _ok(_call(server, "session.turn.start", {"session_id": session_id, "message": "hola"}))
    task = _here(server, session_id)
    [run_id] = server._schedule.tick(_due(server, task["id"]))
    for _ in range(8):  # more than any page of recent runs
        [skipped] = server._schedule.tick(_due(server, task["id"]))
        assert _run(server, skipped)["reason"] == "previous_run_active"
    assert server.turns.queue_list(session_id)["pending"] == 1
    gate.set()
    assert _finished(server, run_id)["status"] == "completed"


def test_a_held_queue_keeps_the_run_waiting(server, monkeypatch) -> None:
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    task = _here(server, session_id)
    monkeypatch.setattr(server.turns, "queue_hold", lambda sid: sid == session_id)
    [run_id] = server._schedule.tick(_due(server, task["id"]))
    assert _run(server, run_id)["status"] == "queued"
    assert not server.turns.has_active_turn(session_id)


def test_deleting_or_switching_off_a_task_takes_its_run_out_of_the_queue(
    server, monkeypatch
) -> None:
    gate = threading.Event()
    fake = FakeModel(["al dueño", "lo otro"], gate=gate)
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    _ok(_call(server, "session.turn.start", {"session_id": session_id, "message": "hola"}))
    _ok(_call(server, "session.queue.add", {"session_id": session_id, "message": "y luego esto"}))
    off = _here(server, session_id, name="Apagada")
    gone = _here(server, session_id, name="Borrada")
    [off_run] = server._schedule.tick(_due(server, off["id"]))
    server._schedule.tick(_due(server, gone["id"]))
    assert server.turns.queue_list(session_id)["pending"] == 3
    _ok(_call(server, "schedule.update", {"task_id": off["id"], "patch": {"enabled": False}}))
    run = _run(server, off_run)
    assert (run["status"], run["reason"]) == ("cancelled", "cancelled_by_owner")
    _ok(_call(server, "schedule.delete", {"task_id": gone["id"]}))
    # Only the owner's own message is still waiting.
    [entry] = server.turns.queue_list(session_id)["entries"]
    assert entry["message"] == "y luego esto"
    gate.set()
    _wait(
        lambda: (
            not server.turns.has_active_turn(session_id)
            and server.turns.queue_list(session_id)["pending"] == 0
        )
    )


def test_a_failed_run_leaves_an_archived_conversation_archived(server, monkeypatch) -> None:
    class Broken:
        def capabilities(self):
            return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

        def invoke(self, request):
            raise RuntimeError("provider down")

    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: Broken())
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    task = _here(server, session_id)
    server._services.sessions.archive(session_id)
    [run_id] = server._schedule.tick(_due(server, task["id"]))
    assert _finished(server, run_id)["status"] == "failed"
    assert server._services.sessions.show(session_id).state == "archived"


def test_a_deleted_conversation_settles_its_waiting_run_at_the_next_tick(server) -> None:
    session_id = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    task = _here(server, session_id, schedule={"kind": "once", "after": {"days": 2}})
    repo = server._services.ctx.schedule_repo
    now = server._services.ctx.clock.now()
    repo.save_run(
        "run_left", task_id=task["id"], status="queued", trigger="manual", session_id=session_id
    )
    server._schedule._recovered = True  # no restart: just a tick while it waits
    server._services.sessions.delete(session_id)
    server._schedule.tick(now)
    run = _run(server, "run_left")
    assert (run["status"], run["reason"]) == ("failed", "conversation_deleted")


def test_after_never_lands_before_the_time_in_a_repeated_hour(monkeypatch) -> None:
    """Where the clock repeats an hour, the local `at` keeps «never earlier»."""
    if not hasattr(time, "tzset"):
        pytest.skip("needs time.tzset to switch the local zone")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        # 01:00 EDT: an hour later the clock reads 01:00 again (EST).
        now = datetime.fromisoformat("2026-11-01T05:00:00+00:00").timestamp()
        at = resolve_after({"kind": "once", "after": {"minutes": 60}}, now)["at"]
        assert datetime.fromisoformat(at).timestamp() >= now + 3600
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_the_capability_is_announced() -> None:
    assert CAPABILITIES["schedule_target_v1"] is True


# -- changing and stopping what was scheduled (owner, 22-oct) ---------------------------


def _tools(services, **host):
    return {
        tool.name: tool
        for tool in schedule_tools(ScheduleToolHost(service=services.schedules, **host))
    }


def test_the_same_name_here_changes_the_task_instead_of_doubling_it(services) -> None:
    """«Cámbialo a cada minuto» left the 30-second radar running next to the new one."""
    session_id = _chat(services)
    tools = _tools(services)
    owner = SimpleNamespace(session_id=session_id, origin_kind="user")
    radar = _task(name="radar-oro", schedule={"kind": "interval", "seconds": 30})
    assert tools["schedule.propose"].handler({**radar, "in_this_conversation": True}, owner).ok
    again = {**radar, "schedule": {"kind": "interval", "minutes": 1}, "in_this_conversation": True}
    result = tools["schedule.propose"].handler(again, owner)
    assert result.ok and result.data["status"] == "updated"
    [task] = services.schedules.list()
    assert task["schedule"] == {"kind": "interval", "minutes": 1}


def test_a_model_lists_changes_and_cancels_only_its_own_tasks(services) -> None:
    session_id = _chat(services)
    owner = SimpleNamespace(session_id=session_id, origin_kind="user")
    mine = services.schedules.create(_task(name="Precio"))
    theirs = services.schedules.create(_task(name="Ajeno"))
    # An Agency agent's rule (its card is not needed for whose it is).
    services.ctx.schedule_repo.save(
        theirs["id"], {**services.schedules.get(theirs["id"]), "agent_id": "agent_x"}
    )
    seen: list[tuple[str, str]] = []
    services.schedules.on_changed = lambda task_id, change: seen.append((task_id, change))
    tools = _tools(services)
    listed = tools["schedule.list"].handler({}, owner)
    assert [item["name"] for item in listed.data["tasks"]] == ["Precio"]
    changed = tools["schedule.update"].handler(
        {"task": "precio", "schedule": {"kind": "interval", "minutes": 5}, "enabled": False}, owner
    )
    assert changed.ok and changed.data["enabled"] is False
    assert services.schedules.get(mine["id"])["schedule"] == {"kind": "interval", "minutes": 5}
    assert not tools["schedule.cancel"].handler({"task": theirs["id"]}, owner).ok
    assert tools["schedule.cancel"].handler({"task": "Precio"}, owner).ok
    assert [task["id"] for task in services.schedules.list()] == [theirs["id"]]
    assert seen == [(mine["id"], "updated"), (mine["id"], "deleted")]
    # An agent reaches only its own rules.
    agent_tools = _tools(services, agent_id="agent_x")
    assert [t["name"] for t in agent_tools["schedule.list"].handler({}, owner).data["tasks"]] == [
        "Ajeno"
    ]


def test_a_task_the_owner_stopped_is_not_switched_back_on_by_a_model(services) -> None:
    task = services.schedules.create(_task(name="Precio"))
    services.schedules.update(task["id"], {"enabled": False})
    services.ctx.schedule_repo.save(
        task["id"], {**services.schedules.get(task["id"]), "paused_by": "stop_all"}
    )
    owner = SimpleNamespace(session_id="ses_x", origin_kind="user")
    result = _tools(services)["schedule.update"].handler({"task": "Precio", "enabled": True}, owner)
    assert not result.ok and "owner" in result.error.message
