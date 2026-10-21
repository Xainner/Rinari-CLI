"""An agent's reasoning effort and icon (migration 0065).

The effort is the one the agent's turns use when the turn does not choose
one: its direct chat, its posts, its rules and its children. The icon is an id
the app maps to an avatar; the Engine only keeps it safe.
"""

from __future__ import annotations

# ruff: noqa: F811
import threading

from tests.unit.test_agency_teams import (  # noqa: F401  (fixtures)
    _agent,
    _call,
    _ok,
    _post,
    _team,
    server,
    services,
)


def _error(response: dict) -> str:
    assert response["ok"] is False, response
    return response["error"]["message"]


def test_the_card_keeps_effort_and_icon_and_checks_them(server) -> None:
    agent = _agent(server, "Emails", reasoning_effort="high", icon="rinari-headset")
    assert agent["reasoning_effort"] == "high" and agent["icon"] == "rinari-headset"
    listed = _ok(_call(server, "agency.agent.list", {}))["agents"]
    assert {(a["reasoning_effort"], a["icon"]) for a in listed} == {("high", "rinari-headset")}
    updated = _ok(
        _call(
            server,
            "agency.agent.update",
            {"agent_id": agent["id"], "patch": {"reasoning_effort": None, "icon": None}},
        )
    )["agent"]
    assert updated["reasoning_effort"] is None and updated["icon"] is None
    bad_effort = _call(
        server, "agency.agent.create", {"agent": {"name": "X", "reasoning_effort": "turbo"}}
    )
    assert "reasoning_effort" in _error(bad_effort)
    for icon in ("../x", "a b", "x" * 41, 7):
        bad_icon = _call(server, "agency.agent.create", {"agent": {"name": "Y", "icon": icon}})
        assert "icon" in _error(bad_icon)


def _starter(server, monkeypatch):
    """Start turns without a model and return the effort each one started with."""
    seen: list = []
    started = threading.Semaphore(0)

    def run_turn(turn_id, record, message, reasoning_effort):
        seen.append(reasoning_effort)
        server._turns._turns[turn_id].done.set()  # nothing runs: the turn is over
        started.release()

    monkeypatch.setattr(server._turns, "_run_turn", run_turn)

    def start(session_id, message, **kwargs):
        server._turns.start_turn(session_id, message, **kwargs)
        assert started.acquire(timeout=5)
        return seen[-1]

    return start


def test_an_agents_turns_use_its_effort_unless_the_turn_chooses(server, monkeypatch) -> None:
    start = _starter(server, monkeypatch)
    writer = _agent(server, "Writer", reasoning_effort="low")
    team = _team(server, "Canal", [writer], connectors=[])
    assert start(writer["session_id"], "hola") == "low"  # its direct chat
    assert start(_post(team, writer), "un recado", origin={"kind": "agency_errand"}) == "low"
    assert start(writer["session_id"], "piensa más", reasoning_effort="high") == "high"
    plain = server._services.sessions.new(cwd=server._services.ctx.layout.root, forced_chat=True)
    assert start(plain.id, "Rinari normal") is None
