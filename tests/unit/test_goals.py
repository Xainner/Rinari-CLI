"""/goal: an objective Rinari pursues turn after turn (plan 0.4.0 #23)."""

from __future__ import annotations

import time

import pytest

from rinari.cli import agent_runtime
from rinari.goals import GoalError
from rinari.goals.service import TurnOutcome
from rinari.models.types import ModelResponse, StopReason, ToolCall
from tests.unit import test_checklist as base
from tests.unit.test_checklist import _call, _done, _Model

services = base.services
server = base.server


@pytest.fixture
def goals(services):
    return services.goals


@pytest.fixture
def session_id(services, tmp_path):
    return services.sessions.start(tmp_path).session.id


def _report(status: str, summary: str, call_id: str, evidence: str = "") -> ModelResponse:
    return ModelResponse(
        content="Anoto cómo va el objetivo.",
        tool_calls=(
            ToolCall(
                call_id,
                "goal.update",
                {"status": status, "summary": summary, "evidence": evidence},
            ),
        ),
        stop_reason=StopReason.TOOL_CALLS,
    )


# -- the service ----------------------------------------------------------------------


def test_a_goal_starts_pursuing_with_no_cap(goals, session_id):
    goal = goals.set(session_id, "  Que   los tests pasen  ")
    assert goal.objective == "Que los tests pasen"
    assert goal.state == "pursuing" and goal.budget == {} and goal.iteration == 0
    with pytest.raises(GoalError):
        goals.set(session_id, "   ")
    with pytest.raises(GoalError):
        goals.set(session_id, "x", {"turns": 0.5})


def test_achieved_needs_the_gate_to_agree(goals, session_id):
    goals.set(session_id, "arreglar el login")
    goals.begin_iteration(session_id)
    goals.report(session_id, "achieved", "login arreglado", "pytest ok")
    gate = {"outcome": "PARTIAL", "reasons": ["typecheck not run"]}
    goal, go_on = goals.after_turn(session_id, TurnOutcome("completed", gate=gate))
    assert go_on and goal.state == "pursuing"
    assert "typecheck not run" in goals.continuation(goal)

    goals.begin_iteration(session_id)
    goals.report(session_id, "achieved", "login arreglado y typecheck ok")
    goal, go_on = goals.after_turn(
        session_id, TurnOutcome("completed", gate={"outcome": "DONE", "reasons": []})
    )
    assert not go_on and goal.state == "achieved"


def test_a_chat_without_a_gate_trusts_the_evidence(goals, session_id):
    goals.set(session_id, "investigar qwen")
    goals.begin_iteration(session_id)
    goals.report(session_id, "achieved", "resumen listo", "fuentes citadas")
    goal, go_on = goals.after_turn(session_id, TurnOutcome("completed"))
    assert goal.state == "achieved" and not go_on


def test_three_quiet_iterations_stall(goals, session_id):
    goals.set(session_id, "algo")
    states = []
    for _ in range(3):
        goals.begin_iteration(session_id)
        goal, go_on = goals.after_turn(session_id, TurnOutcome("completed"))
        states.append((goal.state, go_on))
    assert states == [("pursuing", True), ("pursuing", True), ("stalled", False)]


def test_files_changed_count_as_progress(goals, session_id):
    goals.set(session_id, "algo")
    for _ in range(4):
        goals.begin_iteration(session_id)
        goal, go_on = goals.after_turn(session_id, TurnOutcome("completed", files_changed=2))
    assert goal.state == "pursuing" and go_on


def test_stop_pauses_and_a_limit_waits(goals, session_id):
    goals.set(session_id, "algo")
    goals.begin_iteration(session_id)
    goal, go_on = goals.after_turn(session_id, TurnOutcome("cancelled"))
    assert (goal.state, goal.reason, go_on) == ("paused", "stopped", False)
    goals.resume(session_id)
    goals.begin_iteration(session_id)
    goal, _ = goals.after_turn(
        session_id, TurnOutcome("failed", error_code="QUOTA_EXHAUSTED", error_message="sin saldo")
    )
    assert goal.state == "waiting_limits" and goal.reason == "sin saldo"


def test_the_owners_cap_ends_it_and_the_last_turn_wraps_up(goals, session_id):
    goals.set(session_id, "algo", {"turns": 2})
    goal = goals.begin_iteration(session_id)
    assert "last iteration" in goals.continuation(goal)
    goal, go_on = goals.after_turn(session_id, TurnOutcome("completed", files_changed=1))
    assert go_on
    goals.begin_iteration(session_id)
    goal, go_on = goals.after_turn(session_id, TurnOutcome("completed", files_changed=1))
    assert (goal.state, go_on) == ("budget_limited", False)


def test_blocked_waits_for_the_owner(goals, session_id):
    goals.set(session_id, "desplegar")
    goals.begin_iteration(session_id)
    goals.report(session_id, "blocked", "falta la clave", remaining="la clave de la API")
    goal, go_on = goals.after_turn(session_id, TurnOutcome("completed"))
    assert (goal.state, goal.reason, go_on) == ("blocked", "la clave de la API", False)


# -- through the Engine -------------------------------------------------------------


def _wait_goal(server, session_id, until, timeout=30.0):
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        seen.extend(server.drain_events())
        goal = _call(server, "session.goal.get", {"session_id": session_id})["result"]["goal"]
        busy = server._turns.has_active_turn(session_id)
        if goal is not None and until(goal) and not busy:
            return goal, seen
        time.sleep(0.05)
    raise AssertionError("the goal did not get there")


def test_a_goal_runs_turn_after_turn_until_rinari_reports_it_achieved(
    server, tmp_path, monkeypatch
):
    session_id = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})["result"][
        "session"
    ]["id"]
    model = _Model(
        [
            _report("progress", "encontré la fuente", "g1"),
            _done("Sigo."),
            _report("achieved", "resumen listo", "g2", evidence="3 fuentes"),
            _done("Listo."),
        ]
    )
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model)
    started = _call(
        server, "session.goal.set", {"session_id": session_id, "objective": "Resumir qwen 4"}
    )
    assert started["ok"], started
    assert started["result"]["turn"]["turn_id"]
    goal, seen = _wait_goal(server, session_id, lambda g: g["state"] != "pursuing")
    assert goal["state"] == "achieved" and goal["iteration"] == 2, goal["reason"]
    first = next(m for m in model.requests[0].messages if (m.origin or {}).get("source") == "goal")
    assert "Resumir qwen 4" in (first.content or "")
    started_turns = [e for e in seen if e.get("event") == "turn.started"]
    assert all(e["payload"]["origin"]["source"] == "goal" for e in started_turns)
    assert any(e.get("event") == "goal.updated" for e in seen)
    # The goal's own tool is not offered once it is achieved.
    assert server._services.goals.pursuing(session_id) is None


def test_pause_lets_the_running_turn_finish_and_starts_nothing(server, tmp_path, monkeypatch):
    session_id = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})["result"][
        "session"
    ]["id"]
    model = _Model([_done("uno"), _done("dos")], block_after=0)
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model)
    _call(server, "session.goal.set", {"session_id": session_id, "objective": "algo largo"})
    assert model.blocking.wait(5)
    paused = _call(server, "session.goal.pause", {"session_id": session_id})["result"]
    assert paused["goal"]["state"] == "paused"
    model.block_after = None
    model.abort.set()
    goal, _ = _wait_goal(server, session_id, lambda g: g["state"] == "paused")
    assert goal["iteration"] == 1
