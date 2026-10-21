"""Rinari Agency A2: teams, posts, the office chat, the board, budgets and templates."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.agency_teams import find_mentions
from rinari.engine_protocol.server import EngineServer
from rinari.storage.records import SessionEventRecord

_IDS = itertools.count()


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


@pytest.fixture
def server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


@pytest.fixture
def turns(server, monkeypatch):
    """Turns that start without a model: what each post was asked, in order."""
    started: list[dict] = []

    def start_turn(session_id, message, **kwargs):
        started.append({"session_id": session_id, "message": message, **kwargs})
        return {"turn_id": f"turn-{len(started)}"}

    monkeypatch.setattr(server._turns, "start_turn", start_turn)
    return started


def _call(server, method, params=None):
    line = {"id": f"tm-{next(_IDS)}", "method": method, "params": params or {}}
    response = server.handle_line(json.dumps(line))
    assert response is not None
    return response


def _ok(response):
    assert response["ok"] is True, response
    return response["result"]


def _agent(server, name, **card):
    return _ok(_call(server, "agency.agent.create", {"agent": {"name": name, **card}}))["agent"]


def _team(server, name, members, **card):
    team = {"name": name, "members": [agent["id"] for agent in members], **card}
    return _ok(_call(server, "agency.team.create", {"team": team}))["team"]


def _post(team, agent):
    return next(post for post in team["posts"] if post["agent_id"] == agent["id"])["session_id"]


def _finish(server, session_id, content="Listo.", name="turn.completed", **payload):
    server._teams._observe(
        {"event": name, "payload": {"session_id": session_id, "content": content, **payload}}
    )


def _events(server, name):
    return [evt["payload"] for evt in server.drain_events() if evt.get("event") == name]


def _invoked(server, session_id, inp, out):
    repo = server._services.ctx.event_repo
    repo.insert(
        SessionEventRecord(
            id=f"evt-tm-{next(_IDS)}",
            session_id=session_id,
            seq=repo.next_seq(session_id),
            type="ModelInvoked",
            payload={
                "context_anchor": {"model": "unpriced-model"},
                "usage": {"input_tokens": inp, "output_tokens": out},
            },
            created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


@pytest.fixture(autouse=True)
def _no_background_board(monkeypatch):
    # The board's next tick runs on a thread after each turn: here, inline.
    import threading

    real = threading.Thread

    class Inline:
        def __init__(self, target=None, name=None, daemon=None, **kwargs):
            self._target = target
            self._name = name or ""
            self._real = (
                None
                if self._name.startswith("agency-board")
                else real(target=target, name=name, daemon=daemon, **kwargs)
            )

        def start(self):
            if self._real is not None:
                return self._real.start()
            if self._name == "agency-board-next":
                self._target()
            return None

        def __getattr__(self, attr):
            return getattr(self._real, attr)

    monkeypatch.setattr("rinari.engine_protocol.agency_teams.threading.Thread", Inline)


# -- teams and posts ---------------------------------------------------------------------


def test_a_team_opens_one_post_per_agent_and_the_first_is_the_chief(server) -> None:
    writer, video = _agent(server, "Writer"), _agent(server, "Video")
    team = _team(server, "Canal infantil", [writer, video], purpose="Historias")
    assert team["members"] == [writer["id"], video["id"]]
    assert team["chief_agent_id"] == writer["id"]
    post = server._services.sessions.show(_post(team, video))
    assert post.agent_id == video["id"] and post.team_id == team["id"]
    # The agent's direct chat is not the post.
    assert video["session_id"] != post.id
    assert server._services.sessions.show(video["session_id"]).team_id is None
    changed = _events(server, "agency.team.changed")
    assert {"team_id": team["id"], "change": "created"} in changed


@pytest.mark.parametrize(
    ("card", "message"),
    [
        ({"name": ""}, "name"),
        ({"name": "X", "chief_agent_id": "agt_none"}, "chief"),
        ({"name": "X", "connectors": ["nope"]}, "connector"),
        ({"name": "X", "secret": 1}, "Unknown fields"),
    ],
)
def test_an_invalid_team_is_refused(server, card, message) -> None:
    refused = _call(server, "agency.team.create", {"team": card})
    assert refused["ok"] is False and message in refused["error"]["message"]


def test_a_team_has_at_most_eight_agents_with_different_names(server) -> None:
    agents = [_agent(server, f"A{i}") for i in range(9)]
    refused = _call(
        server,
        "agency.team.create",
        {"team": {"name": "Grande", "members": [a["id"] for a in agents]}},
    )
    assert refused["ok"] is False
    team = _team(server, "Ocho", agents[:8])
    full = _call(server, "agency.team.join", {"team_id": team["id"], "agent_id": agents[8]["id"]})
    assert full["ok"] is False and "another team" in full["error"]["message"]
    twin = _agent(server, "a0")
    small = _team(server, "Dos", [agents[0]])
    same = _call(server, "agency.team.join", {"team_id": small["id"], "agent_id": twin["id"]})
    assert same["ok"] is False and "share a name" in same["error"]["message"]


def test_the_chief_must_be_a_member_and_moves_when_it_leaves(server, turns) -> None:
    writer, video = _agent(server, "Writer"), _agent(server, "Video")
    team = _team(server, "Canal", [writer, video], chief_agent_id=video["id"])
    assert team["chief_agent_id"] == video["id"]
    outsider = _agent(server, "Fuera")
    bad = _call(
        server,
        "agency.team.update",
        {"team_id": team["id"], "patch": {"chief_agent_id": outsider["id"]}},
    )
    assert bad["ok"] is False
    left = _ok(_call(server, "agency.team.leave", {"team_id": team["id"], "agent_id": video["id"]}))
    assert left["team"]["chief_agent_id"] == writer["id"]
    assert left["team"]["members"] == [writer["id"]]


# -- the office chat -----------------------------------------------------------------------


def test_mentions_are_read_by_the_longest_name_and_in_order() -> None:
    names = {"content": "a1", "content manager": "a2", "video": "a3"}
    assert find_mentions("Hola @Video y @Content Manager", names) == ["a3", "a2"]
    assert find_mentions("correo@video.com sin mención", names) == []
    assert find_mentions("@Content, ¿vienes?", names) == ["a1"]


def test_a_message_without_a_mention_wakes_nobody(server, turns) -> None:
    writer = _agent(server, "Writer")
    team = _team(server, "Canal", [writer])
    message = _ok(
        _call(server, "agency.office.post", {"team_id": team["id"], "text": "Buenos días"})
    )["message"]
    assert message["author_kind"] == "owner" and message["mentions"] == []
    assert turns == []


def test_a_mention_wakes_the_agent_in_its_post_and_its_answer_goes_to_the_office(
    server, turns
) -> None:
    writer, video = _agent(server, "Writer"), _agent(server, "Video")
    team = _team(server, "Canal", [writer, video])
    _ok(_call(server, "agency.office.post", {"team_id": team["id"], "text": "@Writer una idea"}))
    assert [turn["session_id"] for turn in turns] == [_post(team, writer)]
    assert "te mencionó" in turns[0]["message"]
    assert "no una instrucción del dueño" not in turns[0]["message"]
    assert turns[0]["origin"]["kind"] == "agency_office"
    # Its answer is posted by the Engine, one hop further, and wakes @Video.
    _finish(server, _post(team, writer), "Un gato astronauta. @Video ¿lo animas?")
    office = _ok(_call(server, "agency.office.list", {"team_id": team["id"]}))["messages"]
    assert office[-1]["author_kind"] == "agent" and office[-1]["agent_id"] == writer["id"]
    assert office[-1]["hop"] == 1 and office[-1]["mentions"] == [video["id"]]
    assert turns[-1]["session_id"] == _post(team, video)
    # What another agent said reaches it as information, not as the owner's word.
    assert "no una instrucción del dueño" in turns[-1]["message"]


def test_a_chain_stops_at_three_hops(server, turns) -> None:
    a, b = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Ping", [a, b])
    _ok(_call(server, "agency.office.post", {"team_id": team["id"], "text": "@Ana empieza"}))
    _finish(server, _post(team, a), "@Beto te toca")  # hop 1
    _finish(server, _post(team, b), "@Ana vuelve")  # hop 2
    _finish(server, _post(team, a), "@Beto otra vez")  # hop 3: nobody wakes
    assert [turn["session_id"] for turn in turns] == [
        _post(team, a),
        _post(team, b),
        _post(team, a),
    ]
    stopped = _events(server, "agency.chain.stopped")
    assert stopped[-1]["reason"] == "too_many_hops" and stopped[-1]["team_id"] == team["id"]


def test_a_message_wakes_at_most_five_agents(server, turns) -> None:
    agents = [_agent(server, f"Ag{i}") for i in range(7)]
    team = _team(server, "Muchos", agents)
    text = " ".join(f"@Ag{i}" for i in range(7))
    _ok(_call(server, "agency.office.post", {"team_id": team["id"], "text": text}))
    assert len(turns) == 5
    assert _events(server, "agency.chain.stopped")[-1]["reason"] == "too_many_mentions"


def test_a_busy_post_takes_its_mentions_one_at_a_time(server, turns) -> None:
    writer = _agent(server, "Writer")
    team = _team(server, "Canal", [writer])
    _ok(_call(server, "agency.office.post", {"team_id": team["id"], "text": "@Writer uno"}))
    _ok(_call(server, "agency.office.post", {"team_id": team["id"], "text": "@Writer dos"}))
    assert len(turns) == 1 and "uno" in turns[0]["message"]
    _finish(server, _post(team, writer), "Hecho uno")
    assert len(turns) == 2 and "dos" in turns[1]["message"]


# -- errands and the board ---------------------------------------------------------------------


def test_an_errand_to_nobody_goes_to_the_chief_and_runs_as_a_turn(server, turns) -> None:
    writer, video = _agent(server, "Writer"), _agent(server, "Video")
    team = _team(server, "Canal", [writer, video])
    errand = _ok(
        _call(
            server,
            "agency.errand.create",
            {
                "team_id": team["id"],
                "errand": {"title": "Guion del lunes", "done_when": ["30 segundos", "Con gancho"]},
            },
        )
    )["errand"]
    assert errand["to_agent_id"] == writer["id"]
    assert turns[0]["session_id"] == _post(team, writer)
    assert "- 30 segundos" in turns[0]["message"] and "Guion del lunes" in turns[0]["message"]
    board = _ok(_call(server, "agency.errand.list", {"team_id": team["id"]}))["errands"]
    assert board[0]["status"] == "in_progress"
    office = _ok(_call(server, "agency.office.list", {"team_id": team["id"]}))["messages"]
    assert office[0]["errand_id"] == errand["id"]


def test_one_errand_per_post_at_a_time_in_board_order(server, turns) -> None:
    writer = _agent(server, "Writer")
    team = _team(server, "Canal", [writer])
    for title in ("Primero", "Segundo"):
        _ok(
            _call(
                server, "agency.errand.create", {"team_id": team["id"], "errand": {"title": title}}
            )
        )
    assert len(turns) == 1 and "Primero" in turns[0]["message"]
    _finish(server, _post(team, writer), "Primero entregado")
    board = {
        item["title"]: item
        for item in _ok(_call(server, "agency.errand.list", {"team_id": team["id"]}))["errands"]
    }
    assert board["Primero"]["status"] == "delivered"
    assert board["Primero"]["result"] == {"summary": "Primero entregado"}
    assert board["Segundo"]["status"] == "in_progress"
    assert len(turns) == 2 and "Segundo" in turns[1]["message"]


def test_a_failed_errand_is_blocked_until_the_owner_retries(server, turns) -> None:
    writer = _agent(server, "Writer")
    team = _team(server, "Canal", [writer])
    errand = _ok(
        _call(server, "agency.errand.create", {"team_id": team["id"], "errand": {"title": "Video"}})
    )["errand"]
    _finish(server, _post(team, writer), None, name="turn.failed", error={"message": "Sin red"})
    blocked = server._services.ctx.team_repo.errand(errand["id"])
    assert blocked["status"] == "blocked" and blocked["return_reason"] == "Sin red"
    server._teams.tick()
    assert len(turns) == 1  # never retried on its own
    _ok(_call(server, "agency.errand.retry", {"errand_id": errand["id"]}))
    assert len(turns) == 2
    again = _call(server, "agency.errand.retry", {"errand_id": errand["id"]})
    assert again["ok"] is False


def test_an_errand_that_cannot_start_is_blocked_with_why(server, monkeypatch) -> None:
    from rinari.engine_protocol.errors import EngineProtocolError

    def refuse(session_id, message, **kwargs):
        raise EngineProtocolError("AGENT_OVER_BUDGET", "Writer used today's budget.")

    monkeypatch.setattr(server._turns, "start_turn", refuse)
    writer = _agent(server, "Writer")
    team = _team(server, "Canal", [writer])
    errand = _ok(
        _call(server, "agency.errand.create", {"team_id": team["id"], "errand": {"title": "Video"}})
    )["errand"]
    blocked = server._services.ctx.team_repo.errand(errand["id"])
    assert blocked["status"] == "blocked" and "budget" in blocked["return_reason"]


def test_a_returned_errand_runs_again_with_the_reason(server, turns) -> None:
    writer = _agent(server, "Writer")
    team = _team(server, "Canal", [writer])
    errand = _ok(
        _call(server, "agency.errand.create", {"team_id": team["id"], "errand": {"title": "Guion"}})
    )["errand"]
    _finish(server, _post(team, writer), "Aquí va")
    no_reason = _call(
        server, "agency.errand.review", {"errand_id": errand["id"], "accepted": False}
    )
    assert no_reason["ok"] is False
    _ok(
        _call(
            server,
            "agency.errand.review",
            {"errand_id": errand["id"], "accepted": False, "reason": "Muy largo"},
        )
    )
    assert "Volvió con este motivo: Muy largo" in turns[-1]["message"]
    _finish(server, _post(team, writer), "Más corto")
    reviewed = _ok(
        _call(server, "agency.errand.review", {"errand_id": errand["id"], "accepted": True})
    )
    assert reviewed["errand"]["status"] == "reviewed"


def test_cancelling_a_running_errand_cancels_its_turn(server, turns, monkeypatch) -> None:
    cancelled = []
    monkeypatch.setattr(server._turns, "cancel_turn", lambda sid: cancelled.append(sid) or {})
    writer = _agent(server, "Writer")
    team = _team(server, "Canal", [writer])
    errand = _ok(
        _call(server, "agency.errand.create", {"team_id": team["id"], "errand": {"title": "Guion"}})
    )["errand"]
    result = _ok(_call(server, "agency.errand.cancel", {"errand_id": errand["id"]}))
    assert result["errand"]["status"] == "cancelled"
    assert cancelled == [_post(team, writer)]
    _finish(server, _post(team, writer), "Tarde")
    assert server._services.ctx.team_repo.errand(errand["id"])["status"] == "cancelled"


def test_errands_of_an_agent_that_leaves_go_to_the_chief_or_are_cancelled(server, turns) -> None:
    writer, video = _agent(server, "Writer"), _agent(server, "Video")
    team = _team(server, "Canal", [writer, video])
    first = _ok(
        _call(server, "agency.errand.create", {"team_id": team["id"], "errand": {"title": "Uno"}})
    )["errand"]
    queued = _ok(
        _call(
            server,
            "agency.errand.create",
            {"team_id": team["id"], "errand": {"title": "Dos", "to_agent_id": video["id"]}},
        )
    )["errand"]
    _finish(server, _post(team, video), None, name="turn.cancelled")
    _ok(_call(server, "agency.team.leave", {"team_id": team["id"], "agent_id": video["id"]}))
    moved = server._services.ctx.team_repo.errand(queued["id"])
    assert moved["to_agent_id"] == writer["id"] and moved["status"] == "pending"
    _ok(_call(server, "agency.team.leave", {"team_id": team["id"], "agent_id": writer["id"]}))
    repo = server._services.ctx.team_repo
    assert repo.errand(queued["id"])["status"] == "cancelled"
    assert repo.errand(first["id"])["status"] == "cancelled"


def test_the_engine_restart_puts_errands_in_progress_back_on_the_board(server, turns) -> None:
    writer = _agent(server, "Writer")
    team = _team(server, "Canal", [writer])
    errand = _ok(
        _call(server, "agency.errand.create", {"team_id": team["id"], "errand": {"title": "Guion"}})
    )["errand"]
    repo = server._services.ctx.team_repo
    assert repo.errand(errand["id"])["status"] == "in_progress"
    server._teams._live.clear()
    server._teams._recovered = False
    server._teams._recover()
    assert repo.errand(errand["id"])["status"] == "pending"


# -- the post's tools ------------------------------------------------------------------------


def _tools(server, session_id):
    from rinari.agency.tools import TeamToolHost, team_tools

    return {tool.name: tool for tool in team_tools(TeamToolHost(server._services, session_id))}


def test_a_post_leaves_errands_to_a_teammate_or_another_team(server, turns) -> None:
    writer, video, fixer = (
        _agent(server, "Writer"),
        _agent(server, "Video"),
        _agent(server, "Fixer"),
    )
    team = _team(server, "Canal", [writer, video])
    upkeep = _team(server, "Mantenimiento", [fixer])
    _ok(_call(server, "agency.office.post", {"team_id": team["id"], "text": "@Writer arranca"}))
    tools = _tools(server, _post(team, writer))
    made = tools["errand.create"].handler(
        {"title": "Animar el guion", "to": "@Video", "done_when": ["9:16"]}, None
    )
    assert made.ok, made
    errand = server._services.ctx.team_repo.errand(made.data["errand_id"])
    assert errand["to_agent_id"] == video["id"] and errand["from_agent_id"] == writer["id"]
    assert errand["hop"] == 1 and errand["chain_id"] == turns[0]["origin"]["chain_id"]
    other = tools["errand.create"].handler(
        {"title": "Se cayó el sitio", "to_team": "mantenimiento"}, None
    )
    assert other.ok
    across = server._services.ctx.team_repo.errand(other.data["errand_id"])
    assert across["team_id"] == upkeep["id"] and across["to_agent_id"] == fixer["id"]
    assert across["from_team_id"] == team["id"]
    unknown = tools["errand.create"].handler({"title": "x", "to": "Nadie"}, None)
    assert unknown.ok is False
    # Delivered with an automatic mention back to whoever asked.
    _finish(server, _post(team, video), "Video listo")
    office = _ok(_call(server, "agency.office.list", {"team_id": team["id"]}))["messages"]
    assert office[-1]["text"].startswith("@Writer Entregado «Animar el guion»")
    review = tools["errand.review"].handler({"errand_id": errand["id"], "accepted": True}, None)
    assert review.ok and review.data["status"] == "reviewed"
    # The board shows open and delivered errands, no longer the reviewed one.
    listed = tools["board.list"].handler({}, None)
    assert listed.ok and listed.data["team"] == "Canal"
    assert "Animar el guion" not in {item["title"] for item in listed.data["errands"]}


def test_errands_between_agents_also_stop_at_three_hops(server, turns) -> None:
    writer, video = _agent(server, "Writer"), _agent(server, "Video")
    team = _team(server, "Canal", [writer, video])
    with server._teams._lock:
        server._teams._live[_post(team, writer)] = {
            "kind": "errand",
            "team_id": team["id"],
            "agent_id": writer["id"],
            "chain_id": "chain-x",
            "hop": 2,
        }
    refused = _tools(server, _post(team, writer))["errand.create"].handler({"title": "Más"}, None)
    assert refused.ok is False
    assert _events(server, "agency.chain.stopped")[-1]["reason"] == "too_many_hops"


def test_office_say_posts_as_the_agent_and_outside_a_post_does_nothing(server, turns) -> None:
    writer, video = _agent(server, "Writer"), _agent(server, "Video")
    team = _team(server, "Canal", [writer, video])
    said = _tools(server, _post(team, writer))["office.say"].handler({"text": "@Video hola"}, None)
    assert said.ok and said.data["mentions"] == 1
    assert turns[-1]["session_id"] == _post(team, video)
    outside = _tools(server, writer["session_id"])["office.say"].handler({"text": "hola"}, None)
    assert outside.ok is False


# -- memory, accounts and budgets ----------------------------------------------------------------


def test_what_a_post_learns_is_its_teams_memory(server) -> None:
    memory = server._services.memory
    writer = _agent(server, "Writer")
    team = _team(server, "Canal", [writer])
    other = _team(server, "Recetas", [_agent(server, "Chef")])
    post = _post(team, writer)
    assert memory.place_for("general", post) == ("team", team["id"])
    assert memory.place_for("agent", post) == ("team", team["id"])
    assert memory.place_for("general", writer["session_id"]) == ("agent", writer["id"])
    memory.remember_user("Animales en 3D", topic="estilo", place="team", place_ref=team["id"])
    memory.remember_user("Sin gluten", topic="estilo", place="team", place_ref=other["id"])
    memory.remember_user("Solo en su chat", topic="x", place="agent", place_ref=writer["id"])
    seen = {row["text"] for row in memory.visible_user_rows("default", post)}
    # A post reads the owner's memory, its own and its team's, never another team's.
    assert {"Animales en 3D", "Solo en su chat"} <= seen
    assert "Sin gluten" not in seen


def test_a_post_sees_its_teams_connectors_and_accounts_only(server) -> None:
    from rinari.mcp.service import STATUS_CONNECTED, _ServerState

    mcp = server._services.mcp
    server._services.agency._connector_exists = lambda name: True
    server._services.teams._connector_exists = lambda name: True
    writer = _agent(server, "Writer", connectors=["gmail"])
    team = _team(server, "Canal", [writer], connectors=["drive"])
    other = _team(server, "Recetas", [_agent(server, "Chef")])
    with mcp._lock:
        for name in ("gmail", "drive"):
            mcp._states[name] = _ServerState(status=STATUS_CONNECTED, tools=[])
        mcp._states["youtube"] = _ServerState(
            status=STATUS_CONNECTED, tools=[], owner=f"team:{team['id']}"
        )
        mcp._states["tiktok"] = _ServerState(
            status=STATUS_CONNECTED, tools=[], owner=f"team:{other['id']}"
        )
        mcp._states["mine"] = _ServerState(
            status=STATUS_CONNECTED, tools=[], owner=f"agent:{writer['id']}"
        )

    def names(session_id):
        return sorted(row["name"] for row in mcp.connectors(None, session_id))

    assert names(_post(team, writer)) == ["drive", "youtube"]
    assert names(writer["session_id"]) == ["gmail", "mine"]


def test_a_team_over_its_budget_stops_its_posts(server) -> None:
    writer = _agent(server, "Writer")
    team = _team(server, "Canal", [writer], budget={"daily_tokens": 100})
    _invoked(server, _post(team, writer), 90, 20)
    refused = _call(
        server, "session.turn.start", {"session_id": _post(team, writer), "message": "Hola"}
    )
    assert refused["ok"] is False and refused["error"]["code"] == "AGENT_OVER_BUDGET"
    assert refused["error"]["details"]["team_id"] == team["id"]
    report = _ok(_call(server, "usage.report", {"scope": "team", "ref": team["id"]}))["report"]
    assert report["totals"]["calls"] == 1


def test_the_agency_settings_hold_the_global_budget_and_the_errand_mode(server) -> None:
    settings = _ok(_call(server, "agency.settings.get"))
    assert settings["errand_mode"] == "turn"
    assert settings["errand_modes"] == ["turn", "subagents"]
    assert settings["available_errand_modes"] == ["turn"]
    later = _call(server, "agency.settings.set", {"errand_mode": "subagents"})
    assert later["ok"] is False and "does not exist yet" in later["error"]["message"]
    saved = _ok(_call(server, "agency.settings.set", {"budget": {"daily_tokens": 100}}))
    assert saved["budget"] == {"daily_tokens": 100}
    writer = _agent(server, "Writer")
    _invoked(server, writer["session_id"], 80, 30)
    assert _ok(_call(server, "agency.settings.get"))["spend_today"]["over_budget"] is True
    refused = _call(
        server, "session.turn.start", {"session_id": writer["session_id"], "message": "Hola"}
    )
    assert refused["ok"] is False and refused["error"]["code"] == "AGENT_OVER_BUDGET"


# -- templates ---------------------------------------------------------------------------------


def test_a_template_copies_the_jobs_and_never_the_accounts(server, turns) -> None:
    writer = _agent(server, "Writer", role="Historias", instructions="Animales 3D.")
    video = _agent(server, "Video")
    team = _team(
        server,
        "Canal infantil",
        [writer, video],
        purpose="Videos para niños",
        chief_agent_id=video["id"],
        budget={"daily_tokens": 5000},
    )
    server._services.memory.remember_user(
        "Clave del canal en el llavero", topic="cuenta", place="team", place_ref=team["id"]
    )
    template = _ok(_call(server, "agency.team.template", {"team_id": team["id"]}))["template"]
    assert template["name"] == "Canal infantil" and template["chief_index"] == 1
    assert template["agents"][0]["instructions"] == "Animales 3D."
    assert "Clave" not in json.dumps(template)
    assert "id" not in template["agents"][0] and "session_id" not in template["agents"][0]
    copy = _ok(
        _call(server, "agency.team.duplicate", {"team_id": team["id"], "name": "Canal de recetas"})
    )["team"]
    assert copy["name"] == "Canal de recetas" and copy["budget"] == {"daily_tokens": 5000}
    assert set(copy["members"]).isdisjoint({writer["id"], video["id"]})
    copied_chief = server._services.agency.get(copy["chief_agent_id"])
    assert copied_chief["name"] == "Video"
    rows = server._services.memory.visible_user_rows("default", _post(copy, copied_chief))
    assert all("Clave" not in row["text"] for row in rows)
    same = _ok(
        _call(
            server,
            "agency.team.duplicate",
            {"team_id": team["id"], "name": "Mismo equipo", "new_agents": False},
        )
    )["team"]
    assert same["members"] == [writer["id"], video["id"]]


def test_archiving_a_team_cancels_its_open_errands(server, turns) -> None:
    writer = _agent(server, "Writer")
    team = _team(server, "Canal", [writer])
    errand = _ok(
        _call(server, "agency.errand.create", {"team_id": team["id"], "errand": {"title": "Guion"}})
    )["errand"]
    _ok(_call(server, "agency.team.archive", {"team_id": team["id"]}))
    assert server._services.ctx.team_repo.errand(errand["id"])["status"] == "cancelled"
    refused = _call(
        server, "agency.errand.create", {"team_id": team["id"], "errand": {"title": "x"}}
    )
    assert refused["ok"] is False
    assert _ok(_call(server, "agency.team.list"))["teams"] == []


def test_a_catalog_account_can_belong_to_one_team(server) -> None:
    team = _team(server, "Canal", [_agent(server, "Writer")])
    added = _ok(
        _call(
            server,
            "mcp.catalog.add",
            {"id": "notion", "name": "notion-del-canal", "owner": f"team:{team['id']}"},
        )
    )
    assert added["server"]["owner"] == f"team:{team['id']}"
    refused = _call(server, "mcp.catalog.add", {"id": "notion", "owner": "team:nadie"})
    assert refused["ok"] is False


def test_a_teams_memory_is_listed_and_written_in_the_agency_settings(server) -> None:
    team = _team(server, "Canal", [_agent(server, "Writer")])
    other = _team(server, "Recetas", [_agent(server, "Chef")])
    added = _ok(
        _call(
            server,
            "agency.team.memory.add",
            {"team_id": team["id"], "topic": "estilo", "text": "Animales en 3D"},
        )
    )["record"]
    assert added["place"] == "team" and added["place_label"] == "Canal"
    server._services.memory.remember_user(
        "Sin gluten", topic="estilo", place="team", place_ref=other["id"]
    )
    listed = _ok(_call(server, "agency.team.memory.list", {"team_id": team["id"]}))
    assert [row["text"] for row in listed["records"]] == ["Animales en 3D"]
    assert _call(server, "agency.team.memory.list", {"team_id": "nadie"})["ok"] is False
