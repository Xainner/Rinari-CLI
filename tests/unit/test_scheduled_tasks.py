"""Scheduled tasks: rules, the task service and the Engine's runner."""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.engine_protocol.server import EngineServer
from rinari.models.types import ModelRequest, ModelResponse, ProviderCapabilities, StopReason
from rinari.policy.approvals import ApprovalRequest, GrantScope
from rinari.schedule.rules import (
    CATCH_UP_WINDOW_S,
    ScheduleError,
    describe,
    next_run,
    parse_schedule,
)
from rinari.schedule.service import ScheduledTaskError
from rinari.shared.errors import CancelledError


def _epoch(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


# -- rules ---------------------------------------------------------------------


def test_daily_and_weekly_pick_the_next_local_time() -> None:
    daily = parse_schedule({"kind": "daily", "time": "08:30"})
    assert next_run(daily, _epoch("2026-09-24T08:00")) == _epoch("2026-09-24T08:30")
    assert next_run(daily, _epoch("2026-09-24T08:30")) == _epoch("2026-09-25T08:30")
    # 2026-09-24 is a Thursday (3): Monday and Wednesday mean next Monday.
    weekly = parse_schedule({"kind": "weekly", "days": [2, 0, 0], "time": "07:05"})
    assert weekly.days == (0, 2)
    assert next_run(weekly, _epoch("2026-09-24T10:00")) == _epoch("2026-09-28T07:05")
    assert describe(weekly) == "Mon, Wed at 07:05"


def test_interval_and_once() -> None:
    every = parse_schedule({"kind": "interval", "minutes": 30})
    assert next_run(every, 1000.0) == 1000.0 + 1800
    once = parse_schedule({"kind": "once", "at": "2026-09-25T09:00"})
    assert next_run(once, _epoch("2026-09-25T08:59")) == _epoch("2026-09-25T09:00")
    assert next_run(once, _epoch("2026-09-25T09:00")) is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"kind": "hourly"},
        {"kind": "interval", "minutes": 1},
        {"kind": "interval", "minutes": True},
        {"kind": "daily", "time": "24:00"},
        {"kind": "daily", "time": "8:30"},
        {"kind": "weekly", "days": [], "time": "08:00"},
        {"kind": "weekly", "days": [7], "time": "08:00"},
        {"kind": "once", "at": "mañana"},
        {"kind": "once", "at": "2026-09-25T09:00+02:00"},
    ],
)
def test_invalid_schedules_are_refused(raw) -> None:
    with pytest.raises(ScheduleError):
        parse_schedule(raw)


# -- service -------------------------------------------------------------------


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
        "name": "Resumen diario",
        "kind": "agent",
        "schedule": {"kind": "daily", "time": "08:30"},
        "prompt": "Resume los cambios de ayer.",
        **overrides,
    }


def test_the_service_validates_and_schedules(services) -> None:
    schedules = services.schedules
    task = schedules.create(
        _task(grants=[{"capability": "shell.exec"}, {"capability": "shell.exec"}])
    )
    assert task["id"].startswith("sch_")
    assert task["next_run_at"] is not None and task["enabled"] is True
    assert task["grants"] == [{"capability": "shell.exec", "target": None}]
    assert task["description"] == "daily at 08:30"
    for bad in (
        {"name": ""},
        {"prompt": "   "},
        {"kind": "script"},
        {"mode": "yolo"},
        {"project_id": "prj_missing"},
        {"model": "no-such-model"},
        {"grants": [{"target": "x"}]},
        {"schedule": {"kind": "interval", "minutes": 2}},
    ):
        with pytest.raises(ScheduledTaskError):
            schedules.create(_task(**bad))

    # Switching it off keeps the next run; back on, it counts from now.
    off = schedules.update(task["id"], {"enabled": False})
    assert off["enabled"] is False and off["next_run_at"] == task["next_run_at"]
    grown = schedules.add_grant(task["id"], "fs.write", "notes.md")
    assert {"capability": "fs.write", "target": "notes.md"} in grown["grants"]
    assert schedules.add_grant(task["id"], "fs.write", "notes.md")["grants"] == grown["grants"]


def test_a_one_time_task_switches_off_after_its_run(services) -> None:
    schedules = services.schedules
    task = schedules.create(
        _task(kind="reminder", schedule={"kind": "once", "at": "2099-01-01T09:00"})
    )
    after = schedules.advance(schedules.get(task["id"]), _epoch("2099-01-01T09:00"))
    assert after["next_run_at"] is None and after["enabled"] is False


# -- the runner ----------------------------------------------------------------


@dataclass
class FakeModel:
    scripted: list[ModelResponse]
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
        return self.scripted.pop(0)


@pytest.fixture
def server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


_IDS = itertools.count()


def _call(server, method, params=None):
    line = {"id": f"sch-{next(_IDS)}", "method": method, "params": params or {}}
    response = server.handle_line(json.dumps(line))
    assert response is not None
    return response


def _ok(response):
    assert response["ok"] is True, response
    return response["result"]


def _events(server, names, until, timeout=30.0):
    seen: list[dict] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        seen.extend(evt for evt in server.drain_events() if evt.get("event") in names)
        if until(seen):
            return seen
        time.sleep(0.02)
    raise AssertionError(f"timed out; saw {[evt['event'] for evt in seen]}")


def _due(server, task_id) -> float:
    return server._services.schedules.get(task_id)["next_run_at"] + 1


def test_a_due_reminder_completes_and_notifies(server) -> None:
    task = _ok(
        _call(server, "schedule.create", {"task": _task(kind="reminder", prompt="Llamar a Ana")})
    )["task"]
    [run_id] = server._schedule.tick(_due(server, task["id"]))
    run = server._services.ctx.schedule_repo.run(run_id)
    assert run["status"] == "completed" and run["summary"] == "Llamar a Ana"
    [done] = _events(server, {"schedule.run.completed"}, lambda seen: seen)
    assert done["payload"]["kind"] == "reminder" and done["payload"]["status"] == "completed"
    # Advanced to tomorrow: the same tick does not run it twice.
    assert server._schedule.tick(_due(server, task["id"]) - 3600) == []


def test_an_agent_run_is_a_turn_in_its_own_session(server, monkeypatch) -> None:
    fake = FakeModel([ModelResponse(content="Todo en orden.", stop_reason=StopReason.END_TURN)])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    task = _ok(
        _call(server, "schedule.create", {"task": _task(grants=[{"capability": "shell.exec"}])})
    )["task"]
    [run_id] = server._schedule.tick(_due(server, task["id"]))
    seen = _events(
        server,
        {"schedule.run.started", "turn.started", "schedule.run.completed"},
        lambda seen: any(evt["event"] == "schedule.run.completed" for evt in seen),
    )
    started = next(evt for evt in seen if evt["event"] == "turn.started")
    assert started["payload"]["origin"] == {
        "kind": "schedule",
        "task_id": task["id"],
        "run_id": run_id,
    }
    run = server._services.ctx.schedule_repo.run(run_id)
    assert run["status"] == "completed" and run["summary"] == "Todo en orden."
    session = server._services.sessions.show(run["session_id"])
    assert session.title.startswith("⏰ Resumen diario · ")
    # The task's grants were seeded as session grants of the run's session.
    [grant] = server.turns.peers.session_grants(run["session_id"])
    assert grant.capability == "shell.exec" and grant.scope is GrantScope.SESSION
    runs = _ok(_call(server, "schedule.get", {"task_id": task["id"]}))["runs"]
    assert [item["id"] for item in runs] == [run_id]


def test_a_run_that_needs_the_owner_says_so_and_grants_join_the_task(server, monkeypatch) -> None:
    gate = threading.Event()
    fake = FakeModel([ModelResponse(content="hecho", stop_reason=StopReason.END_TURN)], gate=gate)
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    task = _ok(_call(server, "schedule.create", {"task": _task()}))["task"]
    [run_id] = server._schedule.tick(_due(server, task["id"]))
    session_id = server._services.ctx.schedule_repo.run(run_id)["session_id"]
    request = ApprovalRequest(
        capability="shell.exec",
        description="run a script",
        target="backup.ps1",
        session_id=session_id,
    )
    answer: dict = {}
    asker = threading.Thread(
        target=lambda: answer.setdefault("v", server.turns._answer_approval(request)), daemon=True
    )
    asker.start()
    try:
        [needs] = _events(server, {"schedule.run.needs_you"}, lambda seen: seen)
        assert needs["payload"]["capability"] == "shell.exec"
        assert needs["payload"]["reason"] == "shell.exec: backup.ps1"
        assert server._services.ctx.schedule_repo.run(run_id)["status"] == "needs_you"
        # «Permitir para esta tarea»: the grant joins the task, by session.
        updated = _ok(
            _call(
                server,
                "schedule.grant",
                {"session_id": session_id, "capability": "shell.exec", "target": "backup.ps1"},
            )
        )["task"]
        assert {"capability": "shell.exec", "target": "backup.ps1"} in updated["grants"]
        _ok(
            _call(
                server,
                "approval.resolve",
                {"approval_id": needs["payload"]["approval_id"], "decision": "allow_session"},
            )
        )
    finally:
        asker.join(timeout=10)
        gate.set()
    _events(server, {"schedule.run.completed"}, lambda seen: seen)
    assert server._services.ctx.schedule_repo.run(run_id)["status"] == "completed"


def test_stale_and_overlapping_runs_are_skipped_and_restarts_recovered(server) -> None:
    repo = server._services.ctx.schedule_repo
    task = _ok(_call(server, "schedule.create", {"task": _task(kind="reminder")}))["task"]
    # A run left open by a previous Engine process is closed on the first tick.
    repo.save_run("run_orphan", task_id=task["id"], status="running", started_at=1.0)
    stale = _due(server, task["id"]) + CATCH_UP_WINDOW_S + 60
    [run_id] = server._schedule.tick(stale)
    assert repo.run("run_orphan")["status"] == "failed"
    assert repo.run("run_orphan")["reason"] == "engine_restarted"
    assert repo.run(run_id)["status"] == "skipped" and repo.run(run_id)["reason"] == "missed"


def test_methods_validate_and_run_now(server) -> None:
    bad = _call(server, "schedule.create", {"task": _task(schedule={"kind": "daily"})})
    assert bad["ok"] is False and bad["error"]["code"] == "INVALID_PARAMS"
    missing = _call(server, "schedule.get", {"task_id": "sch_nope"})
    assert missing["ok"] is False
    task = _ok(_call(server, "schedule.create", {"task": _task(kind="reminder")}))["task"]
    ran = _ok(_call(server, "schedule.run_now", {"task_id": task["id"]}))["run"]
    assert ran["trigger"] == "manual" and ran["status"] == "completed"
    # Running by hand does not move the schedule.
    assert server._services.schedules.get(task["id"])["next_run_at"] == task["next_run_at"]
    listed = _ok(_call(server, "schedule.list"))["tasks"]
    assert listed[0]["last_run"]["id"] == ran["id"]
    assert _ok(_call(server, "schedule.delete", {"task_id": task["id"]}))["deleted"] is True
    assert _ok(_call(server, "schedule.list"))["tasks"] == []


def test_the_model_only_proposes_and_never_grants(services) -> None:
    from types import SimpleNamespace

    from rinari.schedule.tools import ScheduleToolHost, schedule_tools

    announced: list[dict] = []
    services.schedules.on_proposed = announced.append
    [tool] = schedule_tools(ScheduleToolHost(service=services.schedules, project_id=None))
    ctx = SimpleNamespace(session_id="ses_1")
    result = tool.handler(
        {**_task(), "grants": [{"capability": "shell.exec"}], "in_this_project": True}, ctx
    )
    assert result.ok is True
    assert result.data["status"] == "proposed"
    assert result.data["proposal"]["grants"] == []
    assert services.schedules.list() == []
    # The desktop learns of it by event, whole (tool results are clipped).
    [event] = announced
    assert event["session_id"] == "ses_1" and event["proposal"]["name"] == "Resumen diario"
    bad = tool.handler({**_task(), "schedule": {"kind": "daily", "time": "25:00"}}, ctx)
    assert bad.ok is False and "HH:MM" in bad.error.message
