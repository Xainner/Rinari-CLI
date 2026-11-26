"""«Arma tu equipo con Rinari»: a description becomes a proposal the owner reviews,
and the confirmed proposal becomes the team, all at once or nothing."""

# The fixtures are imported from test_agency_teams, so pytest sees them as arguments.
# ruff: noqa: F811

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from rinari.agency.plan import (
    Inventory,
    PlanError,
    parse_json,
    pending_pieces,
    questions_of,
    validate_plan,
)
from rinari.agency.templates import templates
from rinari.models.types import ModelResponse, StopReason
from tests.unit.test_agency_teams import (  # noqa: F401  (fixtures)
    _agent,
    _call,
    _no_background_board,
    _ok,
    server,
    services,
    turns,
)

EVERY_DAY = {"kind": "daily", "time": "09:00"}


@dataclass
class _Caller:
    answers: list[str]
    model_id: str = "fake"

    def __post_init__(self) -> None:
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return ModelResponse(content=self.answers.pop(0), stop_reason=StopReason.END_TURN)


def _event(server, names, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in server.drain_events():
            if evt.get("event") in names:
                return evt
        time.sleep(0.02)
    raise AssertionError(f"no {names}")


def _plan(**overrides):
    plan = {
        "team": {"name": "Facturas", "purpose": "Ordenar facturas", "chief": "lector"},
        "agents": [
            {"key": "lector", "name": "Lector", "role": "Lee", "instructions": "Lee."},
            {"key": "archivo", "name": "Archivo", "role": "Guarda", "instructions": "Guarda."},
        ],
        "flow": {
            "name": "Factura",
            "steps": [
                {"name": "Leer", "agents": ["lector"], "instructions": "Lee la factura"},
                {"name": "Guardar", "agents": ["@Archivo"], "instructions": "Guárdala"},
            ],
        },
        "routines": [
            {
                "name": "Cada día",
                "schedule": EVERY_DAY,
                "starts_flow": True,
                "input": "Las facturas nuevas",
            }
        ],
    }
    plan.update(overrides)
    return plan


# -- the proposal, checked -----------------------------------------------------------------


def test_a_proposal_keeps_only_what_the_inventory_has() -> None:
    inventory = Inventory(
        connectors={
            "correo": {"status": "connected", "about": "", "tools": []},
            "mi-github": {"status": "idle", "about": "", "tools": []},
        },
        catalog={"notion": {"name": "Notion", "description": ""}},
        catalog_added={"github": "mi-github"},
        tool_groups={"web": ["web.fetch"]},
    )
    raw = _plan()
    raw["agents"][0]["uses"] = [
        {"kind": "connector", "name": "correo"},
        {"kind": "catalog", "id": "notion"},
        {"kind": "catalog", "id": "github"},
        {"kind": "connector", "name": "drive-inventado"},
        {"kind": "tools", "group": "web"},
        {"kind": "tools", "group": "no-such-group"},
        {"kind": "missing", "need": "enviar SMS"},
    ]
    plan, notes = validate_plan(raw, inventory)
    uses = plan["agents"][0]["uses"]
    assert {"kind": "connector", "name": "correo"} in uses
    assert {"kind": "catalog", "id": "notion", "name": "Notion"} in uses
    # Already added from the catalog: it is the owner's connector.
    assert {"kind": "connector", "name": "mi-github"} in uses
    # Never kept as a connector that does not exist: a missing piece.
    assert {"kind": "missing", "need": "drive-inventado"} in uses
    assert {"kind": "tools", "group": "web"} in uses
    assert not any(u.get("group") == "no-such-group" for u in uses)
    assert any("drive-inventado" in note for note in notes)
    # Checked again (the review sends it back): the same, by the catalog id.
    again, _ = validate_plan(plan, inventory)
    assert again["agents"][0]["uses"] == uses
    assert [p["kind"] for p in pending_pieces(plan)] == ["catalog", "missing", "missing"]


def test_the_chief_is_one_of_the_agents_and_steps_name_them() -> None:
    plan, _ = validate_plan(_plan(), Inventory())
    assert plan["team"]["chief"] == "lector"
    assert [s["agents"] for s in plan["flow"]["steps"]] == [["lector"], ["archivo"]]
    assert all(s["approval"] == "auto" for s in plan["flow"]["steps"])
    no_chief = _plan(team={"name": "X", "chief": "nadie"})
    assert validate_plan(no_chief, Inventory())[0]["team"]["chief"] == "lector"


def test_a_slip_drops_its_part_and_never_the_whole_proposal() -> None:
    raw = _plan(
        routines=[
            {"name": "Mal", "schedule": {"kind": "daily", "time": "25:99"}, "starts_flow": True},
            {"name": "Sin agente", "schedule": EVERY_DAY, "prompt": "Algo"},
            {"name": "Revisa", "schedule": EVERY_DAY, "agent": "Archivo", "prompt": "Revisa"},
        ]
    )
    raw["team"]["work_folders"] = ["facturas", "C:/Facturas" if True else ""]
    raw["team"]["project_folder"] = "relativa/no"
    plan, notes = validate_plan(raw, Inventory())
    assert [r["name"] for r in plan["routines"]] == ["Revisa"]
    assert plan["routines"][0]["agent"] == "archivo"
    assert plan["team"]["work_folders"] == ["C:/Facturas"]
    assert plan["team"]["project_folder"] is None
    assert len(notes) >= 4
    with pytest.raises(PlanError):
        validate_plan({"agents": []}, Inventory())
    with pytest.raises(PlanError):
        validate_plan({"team": {}, "agents": [{"name": "A"}]}, Inventory())


def test_questions_and_json_in_prose_are_read() -> None:
    assert questions_of(parse_json('Aquí:\n```json\n{"questions": ["¿Dónde?"]}\n```')) == [
        "¿Dónde?"
    ]
    assert questions_of({"questions": ["a", "b", "c", "d"]}) == ["a", "b", "c"]
    with pytest.raises(PlanError):
        parse_json("nada")


def test_an_agent_the_owner_has_is_reused_or_suggested() -> None:
    inventory = Inventory(agents={"agt_1": {"name": "Revisora", "role": "", "connectors": []}})
    raw = _plan()
    raw["agents"][0]["reuse"] = "Revisora"
    raw["agents"][1]["suggest_reuse"] = "revisora"
    plan, _ = validate_plan(raw, inventory)
    first, second = plan["agents"]
    assert first["reuse_agent_id"] == "agt_1" and first["reuse"] is True
    # Suggested: the owner decides before anything is created.
    assert second["reuse_agent_id"] == "agt_1" and second["reuse"] is None
    raw["agents"][1] = {**second, "reuse": False}
    assert (
        validate_plan({**plan, "agents": [first, raw["agents"][1]]}, inventory)[0]["agents"][1][
            "reuse"
        ]
        is False
    )


def test_every_template_is_a_proposal_too() -> None:
    for language in ("en", "es"):
        for item in templates(language):
            plan, notes = validate_plan(item["plan"], Inventory())
            assert notes == [], (item["id"], notes)
            assert plan["flow"] and plan["team"]["manual"]
            assert plan["team"]["chief"] == plan["agents"][0]["key"]
            assert all(step["approval"] == "auto" for step in plan["flow"]["steps"])


# -- the draft -----------------------------------------------------------------------------


def test_the_proposal_arrives_as_an_event_after_one_repair_and_creates_nothing(
    server, monkeypatch
) -> None:
    import json

    caller = _Caller(["No sé hacer eso.", json.dumps(_plan())])
    monkeypatch.setattr(server._planner, "_caller", lambda ref: caller)
    started = _ok(_call(server, "agency.team.draft", {"description": "ordena mis facturas"}))
    ready = _event(server, {"agency.team_draft.ready", "agency.team_draft.failed"})
    assert ready["event"] == "agency.team_draft.ready", ready
    assert ready["payload"]["job_id"] == started["job_id"]
    assert ready["payload"]["kind"] == "plan"
    assert ready["payload"]["plan"]["team"]["name"] == "Facturas"
    # One repair round: the model was told what was wrong with its first answer.
    assert len(caller.requests) == 2
    assert "cannot be used" in caller.requests[1].messages[-1].content
    # Providers that route by session (OpenCode Go) need one on every call.
    assert {request.session_id for request in caller.requests} == {started["job_id"]}
    # The description goes as the owner's message, the inventory in the system one.
    assert "ordena mis facturas" in caller.requests[0].messages[1].content
    assert "inventory" in caller.requests[0].messages[0].content.lower()
    assert server._services.agency.list(None) == []
    assert _ok(_call(server, "agency.team.list"))["teams"] == []


def test_unclear_work_gets_questions_and_a_second_failure_is_said(server, monkeypatch) -> None:
    caller = _Caller(['{"questions": ["¿En qué carpeta?"]}'])
    monkeypatch.setattr(server._planner, "_caller", lambda ref: caller)
    _ok(_call(server, "agency.team.draft", {"description": "guárdalas en mi carpeta"}))
    asked = _event(server, {"agency.team_draft.ready", "agency.team_draft.failed"})
    assert asked["payload"]["kind"] == "questions"
    assert asked["payload"]["questions"] == ["¿En qué carpeta?"]

    monkeypatch.setattr(server._planner, "_caller", lambda ref: _Caller(["no", "tampoco"]))
    _ok(_call(server, "agency.team.draft", {"description": "algo"}))
    failed = _event(server, {"agency.team_draft.ready", "agency.team_draft.failed"})
    assert failed["event"] == "agency.team_draft.failed"


def test_a_draft_needs_a_description_and_a_model(server, monkeypatch) -> None:
    assert _call(server, "agency.team.draft", {"description": " "})["ok"] is False
    monkeypatch.setattr(server._services.providers, "current", lambda: None)
    refused = _call(server, "agency.team.draft", {"description": "un equipo"})
    assert refused["ok"] is False and "provider" in refused["error"]["message"]


# -- creating it ---------------------------------------------------------------------------


def test_apply_creates_the_team_its_flow_and_routine_with_the_chief(server, tmp_path) -> None:
    out = tmp_path / "salidas" / "facturas"
    raw = _plan()
    raw["team"]["work_folders"] = [str(out)]
    result = _ok(_call(server, "agency.team.apply", {"plan": raw}))
    team = result["team"]
    agents = {a["name"]: a for a in server._services.agency.list(None)}
    assert set(agents) == {"Lector", "Archivo"}
    assert team["chief_agent_id"] == agents["Lector"]["id"]
    assert set(team["members"]) == {agents["Lector"]["id"], agents["Archivo"]["id"]}
    # The named folder exists and is trusted: the team writes there without asking.
    assert out.is_dir() and team["folders"] == [str(out.resolve())]
    assert server._services.trust.status(out).state == "trusted"
    flows = _ok(_call(server, "agency.flow.list", {"team_id": team["id"]}))["flows"]
    assert [f["name"] for f in flows] == ["Factura"]
    assert flows[0]["steps"][1]["agents"] == [agents["Archivo"]["id"]]
    routines = _ok(_call(server, "agency.routine.list", {"team_id": team["id"]}))["routines"]
    (routine,) = routines
    assert routine["kind"] == "flow" and routine["enabled"] is True
    assert routine["flow"]["id"] == flows[0]["id"]
    assert result["setup"] == []


def test_the_project_folder_becomes_the_teams_project(server, tmp_path) -> None:
    folder = tmp_path / "repo"
    raw = _plan()
    raw["team"]["project_folder"] = str(folder)
    team = _ok(_call(server, "agency.team.apply", {"plan": raw}))["team"]
    project = server._services.projects.get(team["project_id"])
    assert project.canonical_root == str(folder.resolve())
    # A second team in the same folder takes the same project.
    raw["team"]["name"] = "Otro"
    again = _ok(_call(server, "agency.team.apply", {"plan": raw}))["team"]
    assert again["project_id"] == team["project_id"]


def test_a_suggested_agent_waits_for_the_owners_choice(server) -> None:
    mine = _agent(server, "Revisora")
    raw = _plan()
    raw["agents"][1]["suggest_reuse"] = "Revisora"
    refused = _call(server, "agency.team.apply", {"plan": raw})
    assert refused["ok"] is False and "reuse" in refused["error"]["message"]
    assert len(server._services.agency.list(None)) == 1
    plan = _ok(_call(server, "agency.team.check", {"plan": raw}))["plan"]
    plan["agents"][1]["reuse"] = True
    result = _ok(_call(server, "agency.team.apply", {"plan": plan}))
    assert mine["id"] in result["team"]["members"]
    assert result["created_agents"] and mine["id"] not in result["created_agents"]
    assert len(server._services.agency.list(None)) == 2


def test_a_failure_undoes_everything_it_made(server, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise ValueError("schedule store is down")

    monkeypatch.setattr(server._teams.work, "create_flow_routine", boom)
    refused = _call(server, "agency.team.apply", {"plan": _plan()})
    assert refused["ok"] is False and "schedule store is down" in refused["error"]["message"]
    assert server._services.agency.list(None, include_archived=True) == []
    assert server._services.teams.list(None, include_archived=True) == []


# -- «To get started» ----------------------------------------------------------------------


def _with_pieces():
    raw = _plan()
    raw["agents"][1]["uses"] = [
        {"kind": "catalog", "id": "notion"},
        {"kind": "missing", "need": "enviar SMS"},
    ]
    return raw


def test_pending_pieces_hold_the_routine_and_the_flow_until_they_are_done(server) -> None:
    result = _ok(_call(server, "agency.team.apply", {"plan": _with_pieces()}))
    team, setup = result["team"], result["setup"]
    assert [(p["kind"], p["status"], p["agent"]) for p in setup] == [
        ("catalog", "pending", "Archivo"),
        ("missing", "pending", "Archivo"),
    ]
    (routine,) = _ok(_call(server, "agency.routine.list", {"team_id": team["id"]}))["routines"]
    assert routine["enabled"] is False
    flow = _ok(_call(server, "agency.flow.list", {"team_id": team["id"]}))["flows"][0]
    started = _call(
        server, "agency.flow.start", {"team_id": team["id"], "flow_id": flow["id"], "input": "x"}
    )
    assert started["ok"] is False

    for piece in setup:
        after = _ok(
            _call(
                server,
                "agency.team.setup.resolve",
                {"team_id": team["id"], "item_id": piece["id"], "action": "manual"},
            )
        )
    assert all(item["status"] == "done" for item in after["setup"])
    (routine,) = _ok(_call(server, "agency.routine.list", {"team_id": team["id"]}))["routines"]
    assert routine["enabled"] is True


def test_a_catalog_piece_is_done_once_the_owner_connects_it(server, monkeypatch) -> None:
    from rinari.mcp import catalog

    team = _ok(_call(server, "agency.team.apply", {"plan": _with_pieces()}))["team"]
    row = {"name": "mi-notion", "owner": "", "enabled": True}
    monkeypatch.setattr(server._services.ctx.mcp_server_repo, "list", lambda scope=None: [row])
    real = catalog.entry_for_row
    monkeypatch.setattr(
        catalog, "entry_for_row", lambda r: {"id": "notion"} if r is row else real(r)
    )
    setup = _ok(_call(server, "agency.team.setup.get", {"team_id": team["id"]}))["setup"]
    assert setup[0]["status"] == "done" and setup[0]["resolution"] == "connected:mi-notion"
    assert setup[1]["status"] == "pending"
    # A connector of everyone reaches this team's posts as the team's.
    assert "mi-notion" in server._services.teams.get(team["id"])["connectors"]


def test_a_flow_routine_starts_the_flow_with_no_model(server, turns) -> None:
    team = _ok(_call(server, "agency.team.apply", {"plan": _plan()}))["team"]
    (routine,) = _ok(_call(server, "agency.routine.list", {"team_id": team["id"]}))["routines"]
    ran = _ok(_call(server, "schedule.run_now", {"task_id": routine["id"]}))
    assert ran
    runs = server._services.ctx.team_repo.flow_runs(team["id"])
    assert len(runs) == 1 and runs[0]["input"] == "Las facturas nuevas"
    # The first step went to its agent's post; no one was asked to start the flow.
    assert len(turns) == 1 and "Las facturas nuevas" in turns[0]["message"]


def test_a_post_writes_in_its_teams_work_folders(server, tmp_path) -> None:
    from rinari.cli.agent_runtime import _team_folders

    out = tmp_path / "salida"
    raw = _plan()
    raw["team"]["work_folders"] = [str(out)]
    team = _ok(_call(server, "agency.team.apply", {"plan": raw}))["team"]
    services = server._services
    post = services.sessions.show(team["posts"][0]["session_id"])
    assert _team_folders(services, post) == (out.resolve(),)
    services.trust.remove(out)
    assert _team_folders(services, post) == ()
