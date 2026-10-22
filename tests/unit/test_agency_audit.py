"""Rinari Agency: the owner's audit of 2026-10-21 (D-31..D-34), each finding with the
test that exercises its fix through the Engine's real code path."""

# The fixtures are imported from test_agency_teams, so pytest sees them as arguments.
# ruff: noqa: F811

from __future__ import annotations

import types
from pathlib import Path

import pytest

from rinari.engine_protocol.agency_teams import resolve_mentions
from tests.unit.test_agency_teams import (  # noqa: F401  (fixtures)
    _agent,
    _call,
    _events,
    _finish,
    _no_background_board,
    _ok,
    _post,
    _team,
    _tools,
    server,
    services,
    turns,
)


def _direct_tools(server, session_id):
    from rinari.agency.tools import TeamToolHost, direct_tools

    return {tool.name: tool for tool in direct_tools(TeamToolHost(server._services, session_id))}


def _office(server, team):
    return _ok(_call(server, "agency.office.list", {"team_id": team["id"]}))["messages"]


def _live(server, session_id, **work):
    with server._teams._lock:
        server._teams._live[session_id] = work


def _propose(server, agent, **task):
    from rinari.schedule.tools import ScheduleToolHost, schedule_tools

    host = ScheduleToolHost(service=server._services.schedules, agent_id=agent["id"])
    tool = schedule_tools(host)[0]
    ctx = types.SimpleNamespace(session_id=agent["session_id"], origin_kind="user")
    return tool.handler(task, ctx)


# -- E1 / D-31: 8 hops, a stopped chain stays in the office, office.say tells the truth ------


def test_office_say_says_which_mentions_were_not_delivered_and_why(server, turns) -> None:
    ana, beto, cris = _agent(server, "Ana"), _agent(server, "Beto"), _agent(server, "Cris")
    team = _team(server, "Canal", [ana, beto, cris])
    _live(
        server,
        _post(team, ana),
        kind="mention",
        team_id=team["id"],
        agent_id=ana["id"],
        chain_id="chain-long",
        hop=7,
    )
    said = _tools(server, _post(team, ana))["office.say"].handler({"text": "@Beto mira"}, None)
    assert said.ok and said.data["posted"] is True
    assert said.data["woken"] == []
    assert said.data["not_woken"] == [{"name": "Beto", "reason": "too_many_hops"}]
    assert "NOT delivered" in said.data["note"] and "@Beto" in said.data["note"]
    assert not any(t["session_id"] == _post(team, beto) for t in turns)
    stopped = _events(server, "agency.chain.stopped")[-1]
    assert stopped["team_id"] == team["id"] and stopped["chain_id"] == "chain-long"
    notice = _office(server, team)[-1]["notice"]
    assert notice == {"kind": "stopped", "reason": "too_many_hops", "chain_id": "chain-long"}


def test_office_say_reports_who_it_woke(server, turns) -> None:
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Canal", [ana, beto])
    _live(
        server,
        _post(team, ana),
        kind="mention",
        team_id=team["id"],
        agent_id=ana["id"],
        chain_id="chain-a",
        hop=0,
    )
    say = _tools(server, _post(team, ana))["office.say"].handler
    first = say({"text": "@Beto revisa esto"}, None)
    assert first.data["woken"] == ["Beto"] and first.data["not_woken"] == []
    assert "note" not in first.data
    again = say({"text": "@Beto y esto"}, None)
    assert again.data["not_woken"] == [{"name": "Beto", "reason": "already_woken"}]


def test_a_stopped_chain_leaves_one_notice_until_the_owner_dismisses_it(server, turns) -> None:
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Canal", [ana, beto])
    for _ in range(3):
        server._teams._stopped(team, "chain-z", "too_many_deliveries")
    notices = [m for m in _office(server, team) if (m.get("notice") or {}).get("kind") == "stopped"]
    assert len(notices) == 1
    status = _ok(_call(server, "agency.status", {}))
    assert status["waiting_for_owner"]["chains"] == 1
    dismissed = _ok(_call(server, "agency.chain.dismiss", {"team_id": team["id"]}))
    assert dismissed["dismissed"] == [notices[0]["id"]]
    assert _ok(_call(server, "agency.status", {}))["waiting_for_owner"]["chains"] == 0
    still = next(m for m in _office(server, team) if m["id"] == notices[0]["id"])
    assert still["notice"]["dismissed_at"]  # it stays in the office, seen


# -- E13: a mention by the start of a teammate's name -----------------------------------------


def test_mentions_resolve_exact_prefix_ambiguous_and_unknown_names() -> None:
    names = {
        "operator de metatrader": "m1",
        "operator de binance": "b1",
        "revisor": "r1",
        "content": "c1",
        "content manager": "c2",
    }
    ids, problems = resolve_mentions("@Revisor y @Content Manager", names)
    assert ids == ["r1", "c2"] and problems == []
    ids, problems = resolve_mentions("@Operator de MetaTrader, ¿listo?", names)
    assert ids == ["m1"] and problems == []
    ids, problems = resolve_mentions("hola @Operator", names)
    assert ids == []
    assert problems == [
        {"name": "Operator", "reason": "ambiguous_name", "candidates": ["b1", "m1"]}
    ]
    ids, problems = resolve_mentions("@Rev mira y @Nadie también", names)
    assert ids == []
    assert [p["reason"] for p in problems] == ["unknown_name", "unknown_name"]
    assert problems[0]["name"] == "Rev"  # a prefix must be whole words
    assert resolve_mentions("correo@revisor.com", names) == ([], [])


def test_a_mention_by_the_first_word_of_a_name_wakes_that_teammate(server, turns) -> None:
    ana = _agent(server, "Ana")
    operator = _agent(server, "Operator de MetaTrader")
    team = _team(server, "Mesa", [ana, operator])
    said = _tools(server, _post(team, ana))["office.say"].handler({"text": "@Operator abre"}, None)
    assert said.data["woken"] == ["Operator de MetaTrader"]
    assert turns[-1]["session_id"] == _post(team, operator)


def test_office_say_lists_the_exact_names_when_a_mention_is_unknown_or_ambiguous(
    server, turns
) -> None:
    ana = _agent(server, "Ana")
    one, two = _agent(server, "Operator de MetaTrader"), _agent(server, "Operator de Binance")
    team = _team(server, "Mesa", [ana, one, two])
    said = _tools(server, _post(team, ana))["office.say"].handler(
        {"text": "@Operator abre y @Nadie cierra"}, None
    )
    assert said.data["woken"] == []
    reasons = {item["name"]: item for item in said.data["not_woken"]}
    assert reasons["Operator"]["reason"] == "ambiguous_name"
    assert reasons["Operator"]["candidates"] == ["Operator de Binance", "Operator de MetaTrader"]
    assert reasons["Nadie"]["reason"] == "unknown_name"
    assert "Operator de MetaTrader" in said.data["note"] and "Ana" in said.data["note"]
    assert turns == []


def test_a_mentions_prompt_lists_the_teammates_exact_names(server, turns) -> None:
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto Largo")
    team = _team(server, "Canal", [ana, beto])
    _ok(_call(server, "agency.office.post", {"team_id": team["id"], "text": "@Ana hola"}))
    assert "Teammates (exact names): Ana, Beto Largo" in turns[-1]["message"]
    assert "also mentions" not in turns[-1]["message"]


def test_agents_woken_by_the_same_message_know_about_each_other(server, turns) -> None:
    """The owner mentioned two agents; the second answered before the first had worked (#11)."""
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Canal", [ana, beto])
    _ok(
        _call(
            server,
            "agency.office.post",
            {"team_id": team["id"], "text": "@Ana investiga y pásale a @Beto el resultado"},
        )
    )
    prompts = {turn["session_id"]: turn["message"] for turn in turns}
    assert "also mentions Beto" in prompts[_post(team, ana)]
    assert "also mentions Ana" in prompts[_post(team, beto)]
    assert "office.wait" in prompts[_post(team, beto)]


# -- E8: a delivery wakes only whoever asked --------------------------------------------------


def test_a_delivery_wakes_only_the_agent_that_asked_and_starts_no_chat(server, turns) -> None:
    ana, beto, cris = _agent(server, "Ana"), _agent(server, "Beto"), _agent(server, "Cris")
    team = _team(server, "Canal", [ana, beto, cris])
    # The owner leaves an errand: the chief (Ana) takes it.
    owners = _ok(
        _call(server, "agency.errand.create", {"team_id": team["id"], "errand": {"title": "Video"}})
    )["errand"]
    assert turns[-1]["session_id"] == _post(team, ana)
    # Ana passes part of it to Beto and ends her turn: delivered to the owner.
    passed = _tools(server, _post(team, ana))["errand.create"].handler(
        {"title": "Guion", "to": "Beto"}, None
    )
    assert passed.ok
    _finish(server, _post(team, ana), "Listo, @Beto y @Cris ayudaron")
    assert server._services.ctx.team_repo.errand(owners["id"])["status"] == "delivered"
    woken = [t["session_id"] for t in turns]
    assert woken.count(_post(team, cris)) == 0  # the owner's delivery woke nobody
    assert turns[-1]["session_id"] == _post(team, beto)  # Beto takes the errand Ana left
    # Beto delivers to Ana, naming Cris: only Ana is woken, to review it.
    _finish(server, _post(team, beto), "Guion hecho con @Cris")
    assert turns[-1]["session_id"] == _post(team, ana)
    assert "[Errand delivered" in turns[-1]["message"]
    assert turns[-1]["origin"]["kind"] == "agency_delivery"
    assert not any(t["session_id"] == _post(team, cris) for t in turns)
    delivered = _office(server, team)[-1]
    assert delivered["mentions"] == [ana["id"]]
    # Her review stays in her post: nothing more in the office, nobody else woken.
    before_office, before_turns = len(_office(server, team)), len(turns)
    _finish(server, _post(team, ana), "Gracias @Beto, @Cris")
    assert len(_office(server, team)) == before_office and len(turns) == before_turns


# -- E2 / E9: what waits for the owner, and the status the app marks posts with ---------------


def test_the_status_counts_what_only_the_owner_can_move(server, turns) -> None:
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Canal", [ana, beto])
    errand = _ok(
        _call(server, "agency.errand.create", {"team_id": team["id"], "errand": {"title": "A"}})
    )["errand"]
    status = _ok(_call(server, "agency.status", {}))
    assert status["session_ids"]["working"] == []  # turns are fakes: none active
    assert status["stopped"] is False
    _finish(server, _post(team, ana), "Hecho")
    status = _ok(_call(server, "agency.status", {}))
    assert status["waiting_for_owner"] == {"errands": 1, "chains": 0}
    assert status["needs_you"] == 1
    _ok(_call(server, "agency.errand.review", {"errand_id": errand["id"], "accepted": True}))
    assert _ok(_call(server, "agency.status", {}))["waiting_for_owner"]["errands"] == 0
    _ok(_call(server, "agency.stop_all", {}))
    assert _ok(_call(server, "agency.status", {}))["stopped"] is True


def test_the_owner_reviews_an_errand_whose_chain_stopped_before_its_asker_was_told(
    server, turns, monkeypatch
) -> None:
    import rinari.engine_protocol.agency_teams as board

    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Canal", [ana, beto])
    _live(
        server,
        _post(team, ana),
        kind="mention",
        team_id=team["id"],
        agent_id=ana["id"],
        chain_id="chain-cap",
        hop=board.MAX_HOPS - 2,
    )
    made = _tools(server, _post(team, ana))["errand.create"].handler(
        {"title": "Último", "to": "Beto"}, None
    )
    assert made.ok
    _finish(server, _post(team, beto), "Entregado")  # its hop reaches the cap: Ana is not told
    errand = server._services.ctx.team_repo.errand(made.data["errand_id"])
    assert errand["status"] == "delivered"
    assert _ok(_call(server, "agency.status", {}))["waiting_for_owner"] == {
        "errands": 1,
        "chains": 1,
    }
    reviewed = _ok(
        _call(
            server,
            "agency.errand.review",
            {"errand_id": errand["id"], "accepted": False, "reason": "Falta el final"},
        )
    )["errand"]
    assert reviewed["status"] == "returned"


# -- E10 / D-32: «Reanudar» puts back what «Detener a todos» stopped --------------------------


def test_resume_restores_what_the_stop_paused_and_keeps_the_owners_own_pauses(
    server, turns, monkeypatch
) -> None:
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Canal", [ana, beto])
    schedules = server._services.schedules
    on = schedules.create(
        {
            "name": "Diario",
            "kind": "reminder",
            "schedule": {"kind": "daily", "time": "08:00"},
            "prompt": "Hola",
            "agent_id": ana["id"],
        }
    )
    mine = schedules.create(
        {
            "name": "Pausada por el dueño",
            "kind": "reminder",
            "schedule": {"kind": "daily", "time": "09:00"},
            "prompt": "Hola",
            "agent_id": ana["id"],
            "enabled": False,
        }
    )
    # Beto works on one errand; a second one waits sent back with its reason.
    first = _ok(
        _call(
            server,
            "agency.errand.create",
            {"team_id": team["id"], "errand": {"title": "Uno", "to_agent_id": beto["id"]}},
        )
    )["errand"]
    second = _ok(
        _call(
            server,
            "agency.errand.create",
            {"team_id": team["id"], "errand": {"title": "Dos", "to_agent_id": beto["id"]}},
        )
    )["errand"]
    repo = server._services.ctx.team_repo
    repo.save_errand(
        second["id"],
        {**repo.errand(second["id"]), "status": "returned", "return_reason": "Más corto"},
    )
    monkeypatch.setattr(server._turns, "cancel_turn", lambda sid, **kw: None)
    _ok(_call(server, "agency.stop_all", {}))
    assert repo.errand(first["id"])["status"] == "blocked"
    assert server._services.ctx.schedule_repo.get(on["id"])["paused_by"] == "agency_stop"
    # D-32: while stopped, a rule's post reaches the office but wakes nobody.
    report: dict = {}
    server._teams.post_message(
        team, "@Beto ping", author_agent_id=ana["id"], chain_id=None, hop=1, report=report
    )
    assert report["not_woken"] == [{"name": "Beto", "reason": "stopped_by_owner"}]
    _finish(server, _post(team, beto), None, name="turn.cancelled")
    resumed = _ok(_call(server, "agency.resume", {}))
    assert resumed["restored_errands"] == 2 and resumed["resumed_rules"] == 1
    assert server._services.ctx.schedule_repo.get(on["id"])["enabled"] is True
    assert server._services.ctx.schedule_repo.get(mine["id"])["enabled"] is False
    restored = repo.errand(second["id"])
    assert restored["status"] == "returned" and restored["return_reason"] == "Más corto"
    assert repo.errand(first["id"])["status"] == "in_progress"  # taken again from the start
    assert turns[-1]["session_id"] == _post(team, beto)


def test_an_owner_edit_keeps_who_paused_a_rule(server) -> None:
    ana = _agent(server, "Ana")
    rule = server._services.schedules.create(
        {
            "name": "Diario",
            "kind": "reminder",
            "schedule": {"kind": "daily", "time": "08:00"},
            "prompt": "Hola",
            "agent_id": ana["id"],
        }
    )
    _ok(_call(server, "agency.stop_all", {}))
    renamed = _ok(
        _call(server, "schedule.update", {"task_id": rule["id"], "patch": {"name": "Otro"}})
    )["task"]
    assert renamed["paused_by"] == "agency_stop"
    off = _ok(
        _call(server, "schedule.update", {"task_id": rule["id"], "patch": {"enabled": False}})
    )["task"]
    assert off["paused_by"] is None  # the owner switched it off himself: resume leaves it


# -- E4: an agent posts in its office from its chat, a rule delivers to an office or a mate ---


def test_an_agent_writes_in_its_teams_office_from_its_direct_chat(server, turns) -> None:
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Mesa de oro", [ana, beto])
    _team(server, "Otra", [_agent(server, "Zoe")])
    tools = _direct_tools(server, ana["session_id"])
    said = tools["office.say"].handler({"team": "mesa de oro", "text": "@Beto revisa"}, None)
    assert said.ok and said.data["woken"] == ["Beto"]
    assert _office(server, team)[-1]["agent_id"] == ana["id"]
    assert turns[-1]["session_id"] == _post(team, beto)
    refused = tools["office.say"].handler({"team": "Otra", "text": "hola"}, None)
    assert refused.ok is False and "Mesa de oro" in refused.error.message
    listed = tools["teams.list"].handler({}, None).data["teams"]
    assert {item["name"]: item["yours"] for item in listed} == {"Mesa de oro": True, "Otra": False}


def test_a_rule_posts_a_reminder_in_its_agents_office(server, turns) -> None:
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Mesa de oro", [ana, beto])
    made = _propose(
        server,
        ana,
        name="Recordatorio",
        kind="reminder",
        schedule={"kind": "interval", "minutes": 30},
        prompt="Revisen el tablero.",
        deliver_to={"kind": "office", "team": "Mesa de oro"},
    )
    assert made.ok, made.error
    task = server._services.ctx.schedule_repo.get(made.data["task_id"])
    assert task["deliver_to"] == {"kind": "office", "team_id": team["id"]}
    run = server._schedule.run_now(task["id"])["run"]
    posted = _office(server, team)[-1]
    assert posted["text"] == "Revisen el tablero." and posted["agent_id"] == ana["id"]
    assert server._services.ctx.schedule_repo.run(run["id"])["status"] == "completed"
    assert turns == []  # no mention: nobody woken


def test_a_rule_delivers_its_runs_answer_to_a_teammate_and_wakes_only_them(server, turns) -> None:
    ana, beto, cris = _agent(server, "Ana"), _agent(server, "Beto"), _agent(server, "Cris")
    team = _team(server, "Mesa", [ana, beto, cris])
    task = server._services.schedules.create(
        {
            "name": "Resumen",
            "kind": "agent",
            "schedule": {"kind": "daily", "time": "08:00"},
            "prompt": "Resume el día",
            "agent_id": ana["id"],
            "deliver_to": {"kind": "agent", "agent": "Beto"},
        }
    )
    assert task["deliver_to"] == {"kind": "agent", "agent_id": beto["id"], "team_id": team["id"]}
    run = server._schedule.run_now(task["id"])["run"]
    session_id = server._services.ctx.schedule_repo.run(run["id"])["session_id"]
    server._schedule._observe(
        {
            "event": "turn.completed",
            "payload": {
                "session_id": session_id,
                "turn_id": turns[-1] and "turn-1",
                "content": "Hoy: tres pedidos, @Cris tiene uno.",
            },
        }
    )
    posted = _office(server, team)[-1]
    assert posted["text"].startswith("@Beto Hoy: tres pedidos")
    assert posted["mentions"] == [beto["id"]]
    assert turns[-1]["session_id"] == _post(team, beto)
    assert not any(t["session_id"] == _post(team, cris) for t in turns)


@pytest.mark.parametrize(
    ("deliver_to", "message"),
    [
        ({"kind": "office", "team": "Otra"}, "a team this agent works in"),
        ({"kind": "agent", "agent": "Zoe"}, "teammate"),
        ({"kind": "agent", "agent": "Ana"}, "not to its own agent"),
        ({"kind": "mail"}, "deliver_to must be"),
    ],
)
def test_a_rules_delivery_must_be_one_of_its_agents_teams_or_mates(
    server, deliver_to, message
) -> None:
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    _team(server, "Mesa", [ana, beto])
    _team(server, "Otra", [_agent(server, "Zoe")])
    refused = _propose(
        server,
        ana,
        name="X",
        kind="reminder",
        schedule={"kind": "interval", "minutes": 30},
        prompt="Hola",
        deliver_to=deliver_to,
    )
    assert refused.ok is False and message in refused.error.message


# -- E5: archiving an agent ----------------------------------------------------------------


def test_archiving_the_chief_leaves_its_teams_and_pauses_its_rules(server, turns) -> None:
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Canal", [ana, beto])
    solo = _team(server, "Solo", [ana])
    rule = server._services.schedules.create(
        {
            "name": "Diario",
            "kind": "reminder",
            "schedule": {"kind": "daily", "time": "08:00"},
            "prompt": "Hola",
            "agent_id": ana["id"],
        }
    )
    errand = _ok(
        _call(server, "agency.errand.create", {"team_id": team["id"], "errand": {"title": "T"}})
    )["errand"]
    assert errand["to_agent_id"] == ana["id"]
    archived = _ok(_call(server, "agency.agent.archive", {"agent_id": ana["id"]}))
    assert sorted(archived["left_teams"]) == sorted([team["id"], solo["id"]])
    assert archived["paused_rules"] == 1
    canal = _ok(_call(server, "agency.team.get", {"team_id": team["id"]}))
    assert canal["team"]["chief_agent_id"] == beto["id"] and canal["team"]["members"] == [
        beto["id"]
    ]
    assert canal["errands"][0]["to_agent_id"] == beto["id"]
    left = canal["messages"][-1]["notice"]
    assert left == {"kind": "left", "agent": "Ana", "chief": "Beto"}
    alone = _ok(_call(server, "agency.team.get", {"team_id": solo["id"]}))
    assert alone["team"]["chief_agent_id"] is None
    assert alone["messages"][-1]["notice"] == {"kind": "left", "agent": "Ana", "chief": None}
    kept = server._services.ctx.schedule_repo.get(rule["id"])
    assert kept["enabled"] is False and kept["paused_by"] == "agent_archived"
    listed = _ok(_call(server, "agency.agent.list", {"archived": True}))["agents"]
    assert [agent["id"] for agent in listed] == [ana["id"]]
    back = _ok(_call(server, "agency.agent.unarchive", {"agent_id": ana["id"]}))["agent"]
    assert back["archived_at"] is None
    assert server._services.teams.teams_of(ana["id"]) == []  # not back in its teams
    assert server._services.ctx.schedule_repo.get(rule["id"])["enabled"] is False
    _ok(_call(server, "agency.stop_all", {}))
    _ok(_call(server, "agency.resume", {}))
    assert server._services.ctx.schedule_repo.get(rule["id"])["enabled"] is False


# -- E6 / D-34: an agent's personality is its own --------------------------------------------


def _system_prompt(services, session_id):
    from rinari.cli.agent_runtime import build_assembler_context
    from rinari.prompts.assembler import PromptAssembler

    context = build_assembler_context(services, services.sessions.show(session_id))
    bundle = PromptAssembler().build(context)
    return f"{bundle.system_prompt}\n{bundle.turn_context}"


def _rinari_soul(services):
    from rinari.prompts.soul_sections import split_soul
    from rinari.runtime.identity import load_active_soul

    return split_soul(load_active_soul(services.ctx.home).text)[0]


def test_an_agent_never_speaks_with_rinaris_soul(server) -> None:
    services = server._services
    ana = _agent(server, "Ana", role="Ventas", instructions="Responde corto y amable.")
    prompt = _system_prompt(services, ana["session_id"])
    soul = _rinari_soul(services)
    assert soul and soul[:200] not in prompt
    assert "You are «Ana»" in prompt and "Responde corto y amable." in prompt
    assert "## Yourself" not in prompt  # Rinari speaking of the app as herself
    assert "Character intensity" not in prompt
    rinari = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    assert soul[:200] in _system_prompt(services, rinari)  # Rinari keeps hers


def test_an_agent_can_speak_with_a_saved_soul(server) -> None:
    from rinari.soul.store import SoulStore

    services = server._services
    SoulStore(services.ctx.home).create(
        "capitana",
        name="Capitana",
        identity="# Capitana\n\nHablas como una capitana de barco, breve y segura.",
    )
    refused = _call(
        server, "agency.agent.create", {"agent": {"name": "Ana", "soul_id": "no-existe"}}
    )
    assert refused["ok"] is False and "Unknown Soul" in refused["error"]["message"]
    ana = _agent(server, "Ana", soul_id="capitana", instructions="Lleva la agenda.")
    assert ana["soul_id"] == "capitana"
    prompt = _system_prompt(services, ana["session_id"])
    assert "Hablas como una capitana de barco" in prompt
    assert "Lleva la agenda." in prompt
    assert _rinari_soul(services)[:200] not in prompt
    cleared = _ok(
        _call(server, "agency.agent.update", {"agent_id": ana["id"], "patch": {"soul_id": None}})
    )["agent"]
    assert cleared["soul_id"] is None
    assert "capitana de barco" not in _system_prompt(services, ana["session_id"])


def test_a_post_and_a_rule_run_also_use_the_agents_persona(server, turns) -> None:
    services = server._services
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Canal", [ana, beto])
    soul = _rinari_soul(services)
    assert soul[:200] not in _system_prompt(services, _post(team, ana))
    rule = services.schedules.create(
        {
            "name": "Run",
            "kind": "agent",
            "schedule": {"kind": "daily", "time": "08:00"},
            "prompt": "Hola",
            "agent_id": ana["id"],
        }
    )
    run = server._schedule.run_now(rule["id"])["run"]
    session_id = services.ctx.schedule_repo.run(run["id"])["session_id"]
    prompt = _system_prompt(services, session_id)
    assert soul[:200] not in prompt and "You are «Ana»" in prompt


# -- E3: Rinari's home is off limits to an agent's conversations ------------------------------


def _scope(home: Path, workspace: Path, **extra):
    from rinari.policy.engine import PermissionProfile, SessionScope

    return SessionScope(
        kind="CHAT",
        root=workspace,
        cwd=workspace,
        profile=PermissionProfile.FULL_ACCESS,
        engine_home=home,
        engine_home_open=(workspace, home / "artifacts" / "ses_me"),
        engine_home_readable=(home / "skills",),
        **extra,
    )


def test_an_agents_tools_cannot_reach_rinaris_home_in_full_access(tmp_path, monkeypatch) -> None:
    from rinari.policy.engine import PolicyAction, PolicyEngine

    home = tmp_path / "rinari-home"
    workspace = home / "workspaces" / "chat_me"
    workspace.mkdir(parents=True)
    (home / "skills").mkdir()
    policy = PolicyEngine()
    scope = _scope(home, workspace)

    def action(capability, **kwargs):
        return policy.decide(capability, scope, **kwargs).action

    assert action("fs.read", path=str(home / "state.db")) is PolicyAction.DENY
    assert action("fs.read", path=str(home / "workspaces" / "chat_other" / "a.txt")) is (
        PolicyAction.DENY
    )
    assert action("fs.read", path=str(home / "artifacts" / "ses_other" / "x")) is PolicyAction.DENY
    assert action("fs.read", path=str(home / "artifacts" / "ses_me" / "x")) is PolicyAction.ALLOW
    assert action("fs.write", path=str(workspace / "notes.md")) is PolicyAction.ALLOW
    assert action("fs.read", path=str(home / "skills" / "pdf" / "SKILL.md")) is PolicyAction.ALLOW
    assert action("fs.write", path=str(home / "skills" / "pdf" / "SKILL.md")) is PolicyAction.DENY
    assert action("fs.read", path="../../state.db") is PolicyAction.DENY
    monkeypatch.setenv("RINARI_AUDIT_HOME", str(home))
    for command in (
        f"python -c \"import sqlite3; sqlite3.connect(r'{home / 'state.db'}')\"",
        "type ..\\..\\state.db",
        "cat ../../state.db",
        "cd .. && dir",
        "python -c \"open('$RINARI_AUDIT_HOME/state.db')\"",
        f'sqlite3 "{home.as_posix()}/state.db" .tables',
    ):
        assert action("shell.exec", command=command) is PolicyAction.DENY, command
    assert action("shell.exec", command="python build.py --out ./dist") is PolicyAction.ALLOW
    skill = home / "skills" / "pdf" / "run.py"
    assert action("shell.exec", command=f"python {skill} > out.txt") is PolicyAction.ALLOW
    assert action("shell.exec", command=f"echo x > {skill}") is PolicyAction.DENY
    elsewhere = _scope(home, workspace, command_cwd=home)
    assert policy.decide("shell.exec", elsewhere, command="dir").action is PolicyAction.DENY


def test_rinaris_own_conversations_keep_todays_behaviour(tmp_path) -> None:
    from rinari.policy.engine import PermissionProfile, PolicyAction, PolicyEngine, SessionScope

    home = tmp_path / "rinari-home"
    home.mkdir()
    scope = SessionScope(
        kind="CHAT", root=tmp_path, cwd=tmp_path, profile=PermissionProfile.FULL_ACCESS
    )
    policy = PolicyEngine()
    assert policy.decide("fs.read", scope, path=str(home / "state.db")).action is (
        PolicyAction.ALLOW
    )
    assert policy.decide("shell.exec", scope, command=f"type {home / 'state.db'}").action is (
        PolicyAction.ALLOW
    )


def test_every_agent_conversation_carries_the_home_guard(server, turns) -> None:
    from rinari.cli.agent_runtime import _engine_home_guard
    from rinari.tools.runtime import scope_from_context

    services = server._services
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Canal", [ana, beto])
    home = services.ctx.layout.root.resolve()
    for session_id in (ana["session_id"], _post(team, ana)):
        record = services.sessions.show(session_id)
        guard = _engine_home_guard(services, record, Path(record.current_cwd))
        assert guard["engine_home"] == home
        assert Path(record.current_cwd).resolve() in guard["engine_home_open"]
        assert services.ctx.layout.artifacts_dir / session_id in guard["engine_home_open"]
        ctx = types.SimpleNamespace(
            kind="CHAT",
            project_root=None,
            cwd=Path(record.current_cwd),
            profile=None,
            user_home=None,
            worktree=None,
            private_roots=(),
            read_profile=None,
            turn_state=None,
            ask_new_destinations=False,
            extra_project_roots=(),
            **guard,
        )
        assert scope_from_context(ctx).engine_home == home
    rinari = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    record = services.sessions.show(rinari)
    assert _engine_home_guard(services, record, Path(record.current_cwd)) == {}


# -- E11: team templates ------------------------------------------------------------------


def test_templates_come_in_the_owners_language_and_make_a_team(server, turns) -> None:
    listed = _ok(_call(server, "agency.templates.list", {"language": "es-MX"}))["templates"]
    ids = {item["id"] for item in listed}
    assert {"repo-maintenance", "content", "research"} <= ids
    repo = next(item for item in listed if item["id"] == "repo-maintenance")
    assert repo["language"] == "es" and repo["team"]["name"] == "Mantenimiento de repo"
    assert all("connectors" not in agent and "budget" not in agent for agent in repo["agents"])
    english = _ok(_call(server, "agency.templates.list", {}))["templates"]
    assert next(i for i in english if i["id"] == "content")["team"]["name"] == "Content"
    made = _ok(
        _call(
            server,
            "agency.team.from_template",
            {"template_id": "research", "language": "es", "model": "fake-one"},
        )
    )
    assert made["team"]["name"] == "Investigación"
    assert len(made["agents"]) == 3 and made["team"]["members"] == [a["id"] for a in made["agents"]]
    assert made["team"]["chief_agent_id"] == made["agents"][0]["id"]
    model = server._services.models.resolve("fake-one").id
    assert {agent["model"] for agent in made["agents"]} == {model}
    active = server._services.rinari_profiles.active_id()
    assert {agent["rinari_profile_id"] for agent in made["agents"]} == {active}
    unknown = _call(server, "agency.team.from_template", {"template_id": "nope"})
    assert unknown["ok"] is False and unknown["error"]["code"] == "NOT_FOUND"


# -- E12: an agent's conversations say what each one is ------------------------------------


def test_an_agents_sessions_say_their_kind(server, turns) -> None:
    services = server._services
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Canal", [ana, beto])
    rule = services.schedules.create(
        {
            "name": "Run",
            "kind": "agent",
            "schedule": {"kind": "daily", "time": "08:00"},
            "prompt": "Hola",
            "agent_id": ana["id"],
        }
    )
    run = server._schedule.run_now(rule["id"])["run"]
    run_session = services.ctx.schedule_repo.run(run["id"])["session_id"]
    sessions = _ok(_call(server, "agency.agent.get", {"agent_id": ana["id"]}))["sessions"]
    kinds = {item["id"]: item["agency"] for item in sessions}
    assert kinds[ana["session_id"]] == {"kind": "direct"}
    assert kinds[_post(team, ana)] == {
        "kind": "post",
        "team_id": team["id"],
        "team_name": "Canal",
    }
    assert kinds[run_session] == {"kind": "rule_run", "task_id": rule["id"], "run_id": run["id"]}


def test_a_review_queued_before_a_restart_is_queued_again(server, turns) -> None:
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Canal", [ana, beto])
    _live(
        server,
        _post(team, ana),
        kind="mention",
        team_id=team["id"],
        agent_id=ana["id"],
        chain_id="chain-r",
        hop=0,
    )
    made = _tools(server, _post(team, ana))["errand.create"].handler(
        {"title": "Datos", "to": "Beto"}, None
    )
    _finish(server, _post(team, beto), "Datos listos")  # Ana is busy: her review waits
    errand = server._services.ctx.team_repo.errand(made.data["errand_id"])
    assert errand["result"]["review_queued"] is True
    # The Engine restarts: what was in memory is gone.
    with server._teams._lock:
        server._teams._mentions.clear()
        server._teams._live.clear()
        server._teams._recovered = False
    server._teams.tick()
    assert turns[-1]["session_id"] == _post(team, ana)
    assert "[Errand delivered" in turns[-1]["message"]


def test_a_turn_that_spoke_with_office_say_does_not_repeat_itself(server, turns) -> None:
    """Each turn posted twice: its office.say and a final answer saying the same (21-oct)."""
    ana, beto = _agent(server, "Ana"), _agent(server, "Beto")
    team = _team(server, "Canal", [ana, beto])
    post = _post(team, ana)
    work = dict(kind="mention", team_id=team["id"], agent_id=ana["id"], chain_id="c1", hop=0)
    _live(server, post, **work)
    _tools(server, post)["office.say"].handler({"text": "@Beto aquí va el dato"}, None)
    server._teams._observe(
        {"event": "turn.completed", "payload": {"session_id": post, "content": "Le pasé el dato"}}
    )
    texts = [m["text"] for m in _office(server, team)]
    assert any("aquí va el dato" in t for t in texts)
    assert not any("Le pasé el dato" in t for t in texts)
    # Without office.say, the final answer is what the office gets.
    _live(server, post, **{**work, "chain_id": "c2"})
    server._teams._observe(
        {"event": "turn.completed", "payload": {"session_id": post, "content": "Listo, revisado"}}
    )
    assert any("Listo, revisado" in m["text"] for m in _office(server, team))


def test_a_post_reads_what_a_teammate_made_and_nothing_outside_its_team(server) -> None:
    """Image made a PNG; Video, in the same team, could not read it (owner, 21-oct)."""
    from dataclasses import replace

    from rinari.artifacts.transfer import import_file
    from rinari.cli.agent_runtime import _teammate_sessions
    from rinari.tools.native.artifact import _path_for
    from rinari.tools.native.fs import _resolve_read

    image, video, other = _agent(server, "Image"), _agent(server, "Video"), _agent(server, "Otro")
    team = _team(server, "Estudio", [image, video])
    _team(server, "Aparte", [other])
    image_post, video_post = _post(team, image), _post(team, video)
    services = server._services
    assert _teammate_sessions(services, video_post) == (image_post,)
    assert _teammate_sessions(services, other["session_id"]) == ()  # a direct chat: no team

    store = services.artifacts
    source = Path(services.ctx.layout.root) / "personaje.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    made = import_file(store, image_post, source)

    from rinari.tools.definition import ToolContext

    fields = {f: None for f in ToolContext.__dataclass_fields__}
    ctx = replace(
        ToolContext(**{**fields, "session_id": video_post, "artifact_root": store._root()}),
        shared_artifact_sessions=(image_post,),
    )
    path, error = _path_for(made.uri(), ctx)
    assert error is None and path.read_bytes() == source.read_bytes()
    read, error = _resolve_read(ctx, made.uri())
    assert error is None and read.is_file()
    outsider = replace(ctx, session_id=other["session_id"], shared_artifact_sessions=())
    _, denied = _path_for(made.uri(), outsider)
    assert denied is not None and denied.error.code.value == "PERMISSION_DENIED"
