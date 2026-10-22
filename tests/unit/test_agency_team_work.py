"""Rinari Agency replan (22-oct), phases 2 to 4: how a team works on its own.

The team's manual reaches every post; routines are its agents' rules in their
posts; flows hand work on step by step with the approval each step names; the
overview shows the team in one call.
"""

# The fixtures are imported from test_agency_teams, so pytest sees them as arguments.
# ruff: noqa: F811

from __future__ import annotations

from tests.unit.test_agency_teams import (  # noqa: F401  (fixtures)
    _agent,
    _call,
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

EVERY_MINUTE = {"kind": "interval", "minutes": 1}


def _studio(server):
    writer, image, video = (_agent(server, name) for name in ("Writer", "Image", "Video"))
    team = _team(server, "Estudio", [writer, image, video], chief_agent_id=writer["id"])
    return team, writer, image, video


def _place(server, team, agent) -> str:
    from rinari.cli.agent_runtime import _agency_place

    services = server._services
    record = services.sessions.show(_post(team, agent))
    return "\n".join(_agency_place(services, record, services.ctx.agency_repo.get(agent["id"])))


def _office(server, team):
    return _ok(_call(server, "agency.office.list", {"team_id": team["id"]}))["messages"]


def _errands(server, team, run_id):
    return [
        item
        for item in server._services.ctx.team_repo.errands(team["id"])
        if item.get("production_run_id") == run_id
    ]


# -- the manual (phase 2) ------------------------------------------------------------------


def test_the_manual_reaches_every_post_and_the_chief_keeps_the_team_set_up(server) -> None:
    team, writer, image, _video = _studio(server)
    manual = "Cada mañana Image revisa las noticias; los guiones pasan por Writer."
    _ok(_call(server, "agency.team.update", {"team_id": team["id"], "patch": {"manual": manual}}))
    assert server._services.ctx.team_repo.get(team["id"])["manual"] == manual
    chief, member = _place(server, team, writer), _place(server, team, image)
    assert manual in chief and manual in member
    assert "team.routine.create" in chief and "flow.save" in chief
    assert "As chief, keep the team set up" not in member
    assert "only the chief answers the owner" in member


def test_a_post_knows_its_periodic_work_is_a_routine(server) -> None:
    team, writer, *_ = _studio(server)
    text = _place(server, team, writer)
    assert "team.routine.create" in text and "flow.start" in text


# -- routines (phase 2) ------------------------------------------------------------------


def test_the_owner_creates_a_routine_and_the_same_name_updates_it(server) -> None:
    team, _writer, image, _video = _studio(server)
    params = {
        "team_id": team["id"],
        "agent_id": "Image",
        "name": "Noticias",
        "schedule": EVERY_MINUTE,
        "prompt": "Revisa las noticias del oro",
    }
    routine = _ok(_call(server, "agency.routine.create", params))["routine"]
    assert routine["agent_id"] == image["id"] and routine["enabled"]
    assert routine["when"] and routine["next_run_at"] is not None
    task = server._services.schedules.get(routine["id"])
    assert task["session_id"] == _post(team, image) and task["target"] == "this_conversation"
    again = {**params, "schedule": {"kind": "interval", "minutes": 5}}
    _ok(_call(server, "agency.routine.create", again))
    listed = _ok(_call(server, "agency.routine.list", {"team_id": team["id"]}))["routines"]
    assert len(listed) == 1 and listed[0]["schedule"]["minutes"] == 5


def test_only_the_chief_sets_a_teammates_routine(server) -> None:
    team, writer, image, video = _studio(server)
    args = {"agent": "Video", "name": "Render", "schedule": EVERY_MINUTE, "prompt": "Renderiza"}
    refused = _tools(server, _post(team, image))["team.routine.create"].handler(args, None)
    assert not refused.ok and "chief" in refused.error.message
    own = _tools(server, _post(team, image))["team.routine.create"].handler(
        {**args, "agent": None, "name": "Mía"}, None
    )
    assert own.ok, own.error
    done = _tools(server, _post(team, writer))["team.routine.create"].handler(args, None)
    assert done.ok and done.data["agent"] == "Video"
    seen = _tools(server, _post(team, video))["team.routines"].handler({}, None)
    assert {(r["name"], r["agent"]) for r in seen.data["routines"]} == {
        ("Mía", "Image"),
        ("Render", "Video"),
    }


# -- flows (phase 3) ------------------------------------------------------------------


def _flow(server, team, steps):
    flow = {"name": "Pieza", "steps": steps}
    return _ok(_call(server, "agency.flow.save", {"team_id": team["id"], "flow": flow}))["flow"]


def test_a_flow_hands_each_step_on_with_what_the_earlier_ones_delivered(server, turns) -> None:
    team, writer, image, video = _studio(server)
    flow = _flow(
        server,
        team,
        [
            {
                "name": "Personaje",
                "agents": ["Image"],
                "instructions": "Dibuja",
                "approval": "auto",
            },
            {"name": "Video", "agents": ["Video"], "instructions": "Anima", "approval": "chief"},
        ],
    )
    assert flow["steps"][0]["agents"] == [image["id"]]
    run = _ok(
        _call(
            server,
            "agency.flow.start",
            {"team_id": team["id"], "flow_id": flow["id"], "input": "Un zorro"},
        )
    )["run"]
    assert run["status"] == "running" and run["step_name"] == "Personaje"
    (first,) = _errands(server, team, run["id"])
    assert first["to_agent_id"] == image["id"] and first["flow_step"] == 0
    assert turns[-1]["session_id"] == _post(team, image)
    assert "Un zorro" in turns[-1]["message"]

    # Auto approval: accepted as delivered, and the next step is left at once.
    _finish(server, _post(team, image), "El zorro ártico dibujado")
    repo = server._services.ctx.team_repo
    assert repo.errand(first["id"])["status"] == "reviewed"
    second = next(e for e in _errands(server, team, run["id"]) if e["flow_step"] == 1)
    assert second["to_agent_id"] == video["id"] and second["from_agent_id"] == writer["id"]
    assert "El zorro ártico dibujado" in second["body"]

    # Chief approval: the chief is woken to review; accepting it finishes the run.
    before = len(turns)
    _finish(server, _post(team, video), "Video listo")
    assert _post(team, writer) in [t["session_id"] for t in turns[before:]]
    review = _tools(server, _post(team, writer))["errand.review"].handler(
        {"errand_id": second["id"], "accepted": True}, None
    )
    assert review.ok, review.error
    done = repo.flow_run(run["id"])
    assert done["status"] == "done" and len(done["outputs"]) == 2
    states = [
        (m.get("notice") or {}).get("state")
        for m in _office(server, team)
        if (m.get("notice") or {}).get("kind") == "flow"
    ]
    assert states == ["started", "step", "done"]
    report = _office(server, team)[-1]
    assert "Video listo" in report["text"] and report["mentions"] == [writer["id"]]


def test_an_owner_step_waits_for_the_owner_and_a_cancelled_run_cancels_its_errands(
    server, turns
) -> None:
    team, _writer, _image, _video = _studio(server)
    _flow(
        server,
        team,
        [
            {"name": "Borrador", "agents": ["Image"], "approval": "owner"},
            {"name": "Final", "agents": ["Video", "Image"], "approval": "auto"},
        ],
    )
    start = {"team_id": team["id"], "flow_id": "Pieza"}
    run = _ok(_call(server, "agency.flow.start", start))["run"]
    (draft,) = _errands(server, team, run["id"])
    _finish(server, draft["session_id"] or _post(team, _image), "Borrador hecho")
    repo = server._services.ctx.team_repo
    assert repo.errand(draft["id"])["status"] == "delivered"
    assert repo.flow_run(run["id"])["step"] == 0
    _ok(_call(server, "agency.errand.review", {"errand_id": draft["id"], "accepted": True}))
    finals = [e for e in _errands(server, team, run["id"]) if e["flow_step"] == 1]
    assert len(finals) == 2
    cancelled = _ok(_call(server, "agency.flow.run.cancel", {"run_id": run["id"]}))["run"]
    assert cancelled["status"] == "cancelled"
    assert {repo.errand(e["id"])["status"] for e in finals} == {"cancelled"}


def test_only_the_chief_saves_a_flow_and_any_post_starts_one(server, turns) -> None:
    team, writer, image, _video = _studio(server)
    steps = [{"name": "Uno", "agents": ["Image"]}]
    refused = _tools(server, _post(team, image))["flow.save"].handler(
        {"name": "Corto", "steps": steps}, None
    )
    assert not refused.ok
    saved = _tools(server, _post(team, writer))["flow.save"].handler(
        {"name": "Corto", "steps": steps}, None
    )
    assert saved.ok, saved.error
    unknown = _tools(server, _post(team, writer))["flow.save"].handler(
        {"name": "Mal", "steps": [{"name": "X", "agents": ["Nadie"]}]}, None
    )
    assert not unknown.ok and "Nadie" in unknown.error.message
    started = _tools(server, _post(team, image))["flow.start"].handler({"flow": "corto"}, None)
    assert started.ok, started.error
    listed = _tools(server, _post(team, image))["flow.list"].handler({}, None)
    assert listed.data["flows"][0]["steps"][0]["agents"] == ["Image"]
    assert listed.data["runs"][0]["status"] == "running"


# -- the overview (phase 4) ------------------------------------------------------------------


def test_the_overview_shows_the_team_in_one_call(server, turns) -> None:
    team, _writer, _image, _video = _studio(server)
    _ok(
        _call(
            server,
            "agency.routine.create",
            {
                "team_id": team["id"],
                "agent_id": "Image",
                "name": "Noticias",
                "schedule": EVERY_MINUTE,
                "prompt": "Revisa",
            },
        )
    )
    _flow(server, team, [{"name": "Uno", "agents": ["Video"]}])
    _ok(_call(server, "agency.office.post", {"team_id": team["id"], "text": "Hola equipo"}))
    view = _ok(_call(server, "agency.team.overview", {"team_id": team["id"]}))
    assert view["team"]["id"] == team["id"] and view["team"]["stopped"] is False
    assert len(view["members"]) == 3 and len(view["routines"]) == 1
    assert view["flows"][0]["steps"][0]["agent_names"] == ["Video"]
    assert view["activity"][0]["line"].endswith("Hola equipo")
    assert set(view["spend"]) == {"today", "week", "agents"}
    assert len(view["spend"]["agents"]) == 3
    assert view["spend"]["week"]["tokens"] == 0
