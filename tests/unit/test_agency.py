"""Rinari Agency A1: agents, their direct chat, status and spend."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime

import pytest

from rinari.agency import AgencyError
from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
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


def _call(server, method, params=None):
    line = {"id": f"ag-{next(_IDS)}", "method": method, "params": params or {}}
    response = server.handle_line(json.dumps(line))
    assert response is not None
    return response


def _ok(response):
    assert response["ok"] is True, response
    return response["result"]


def _invoked(server, session_id, inp, out, model="unpriced-model"):
    repo = server._services.ctx.event_repo
    repo.insert(
        SessionEventRecord(
            id=f"evt-ag-{next(_IDS)}",
            session_id=session_id,
            seq=repo.next_seq(session_id),
            type="ModelInvoked",
            payload={
                "context_anchor": {"model": model},
                "usage": {"input_tokens": inp, "output_tokens": out},
            },
            created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


# -- the card ------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"name": "   "},
        {"name": "x" * 61},
        {"name": "Emails", "permission_profile": "everything"},
        {"name": "Emails", "budget": {"daily_usd": 0}},
        {"name": "Emails", "budget": {"daily_tokens": 1.5}},
        {"name": "Emails", "budget": {"monthly_usd": 3}},
        {"name": "Emails", "connectors": ["not-connected"]},
        {"name": "Emails", "model": "no-such-model"},
        {"name": "Emails", "ask_new_destinations": "yes"},
        {"name": "Emails", "soul": "free text"},
    ],
)
def test_an_invalid_card_is_refused(services, raw) -> None:
    with pytest.raises(AgencyError):
        services.agency.create(raw)


def test_a_card_is_normalized_and_kept(services) -> None:
    agent = services.agency.create(
        {
            "name": "  Emails ",
            "role": "Lee el correo",
            "instructions": "Reenvía lo importante.",
            "model": "fake-one",
            "permission_profile": "full-access",
            "budget": {"daily_usd": 2, "daily_tokens": 50_000},
            "ask_new_destinations": True,
        },
        "default",
    )
    assert agent["id"].startswith("agent_")
    assert agent["name"] == "Emails"
    assert agent["budget"] == {"daily_usd": 2.0, "daily_tokens": 50_000}
    assert agent["ask_new_destinations"] is True
    assert services.agency.list("default") == [agent]
    assert services.agency.list("other") == []


def test_an_archived_agent_is_kept_but_cannot_change(services) -> None:
    agent = services.agency.create({"name": "Guardia"})
    archived = services.agency.archive(agent["id"])
    assert archived["archived_at"] is not None
    assert services.agency.list(None) == []
    assert services.agency.list(None, include_archived=True)[0]["id"] == agent["id"]
    with pytest.raises(AgencyError):
        services.agency.update(agent["id"], {"role": "otro"})


# -- the direct chat -------------------------------------------------------------


def test_creating_an_agent_opens_its_direct_chat_apart_from_rinari(server) -> None:
    rinari = _ok(_call(server, "session.create", {"chat": True}))["session"]
    created = _ok(
        _call(
            server,
            "agency.agent.create",
            {"agent": {"name": "Emails", "permission_profile": "read-only", "model": "fake-one"}},
        )
    )["agent"]
    chat = server._services.sessions.show(created["session_id"])
    assert chat.kind == "CHAT" and chat.agent_id == created["id"]
    assert chat.permission_profile == "read-only"
    assert chat.title == "Emails"
    listed = _ok(_call(server, "session.list"))["sessions"]
    assert [item["id"] for item in listed] == [rinari["id"]]
    mine = _ok(_call(server, "session.list", {"agent_id": created["id"]}))["sessions"]
    assert [item["id"] for item in mine] == [created["session_id"]]
    assert mine[0]["agent_id"] == created["id"]
    detail = _ok(_call(server, "agency.agent.get", {"agent_id": created["id"]}))
    assert [item["id"] for item in detail["sessions"]] == [created["session_id"]]
    events = [evt for evt in server.drain_events() if evt.get("event") == "agency.agent.changed"]
    assert events[-1]["payload"] == {"agent_id": created["id"], "change": "created"}


def test_the_direct_chat_follows_the_card(server) -> None:
    agent = _ok(_call(server, "agency.agent.create", {"agent": {"name": "Arreglos"}}))["agent"]
    updated = _ok(
        _call(
            server,
            "agency.agent.update",
            {"agent_id": agent["id"], "patch": {"permission_profile": "full-access"}},
        )
    )["agent"]
    assert updated["permission_profile"] == "full-access"
    chat = server._services.sessions.show(agent["session_id"])
    assert chat.permission_profile == "full-access"


def test_methods_validate_their_params(server) -> None:
    assert _call(server, "agency.agent.create", {"agent": "Emails"})["ok"] is False
    assert _call(server, "agency.agent.create", {"agent": {"name": ""}})["ok"] is False
    assert _call(server, "agency.agent.get", {"agent_id": "agent_missing"})["ok"] is False
    assert _call(server, "agency.agent.update", {"agent_id": "x"})["ok"] is False
    assert _call(server, "session.list", {"agent_id": 3})["ok"] is False


# -- status and spend -------------------------------------------------------------


def test_status_counts_the_agents_of_the_profile(server) -> None:
    _ok(_call(server, "agency.agent.create", {"agent": {"name": "Uno"}}))
    _ok(_call(server, "agency.agent.create", {"agent": {"name": "Dos"}}))
    status = _ok(_call(server, "agency.status", {"rinari_profile_id": "active"}))
    assert status["agents"] == 2
    assert status["working"] == 0 and status["needs_you"] == 0


def test_spend_counts_only_the_agents_conversations(server) -> None:
    rinari = _ok(_call(server, "session.create", {"chat": True}))["session"]
    _invoked(server, rinari["id"], 90_000, 9_000)
    agent = _ok(
        _call(
            server,
            "agency.agent.create",
            {"agent": {"name": "Emails", "budget": {"daily_tokens": 1_000}}},
        )
    )["agent"]
    # A new agent spent nothing, even if Rinari did.
    assert agent["spend_today"]["tokens"] == 0
    assert agent["spend_today"]["over_budget"] is False
    _invoked(server, agent["session_id"], 800, 300)
    spend = _ok(_call(server, "agency.agent.get", {"agent_id": agent["id"]}))["agent"][
        "spend_today"
    ]
    assert spend["tokens"] == 1_100 and spend["calls"] == 1
    # A model without a known price: the cost is unknown, never invented.
    assert spend["cost_known"] is False and spend["cost_usd"] is None
    assert spend["over_budget"] is True


def test_the_usage_report_has_an_agent_scope(server) -> None:
    agent = _ok(_call(server, "agency.agent.create", {"agent": {"name": "Sola"}}))["agent"]
    other = _ok(_call(server, "session.create", {"chat": True}))["session"]
    _invoked(server, other["id"], 5_000, 500)
    report = _ok(_call(server, "usage.report", {"scope": "agent", "ref": agent["id"]}))["report"]
    assert report["totals"]["calls"] == 0
    assert _call(server, "usage.report", {"scope": "agent"})["ok"] is False


# -- memory (D-7) -----------------------------------------------------------------


def test_what_an_agent_learns_is_its_own_memory(server) -> None:
    memory = server._services.memory
    agent = _ok(_call(server, "agency.agent.create", {"agent": {"name": "Writer"}}))["agent"]
    other = _ok(_call(server, "agency.agent.create", {"agent": {"name": "Video"}}))["agent"]
    rinari = _ok(_call(server, "session.create", {"chat": True}))["session"]
    chat = agent["session_id"]
    assert memory.place_for("general", chat) == ("agent", agent["id"])
    assert memory.place_for("profile", chat) == ("agent", agent["id"])
    assert memory.place_for("chat", chat) == ("chat", chat)
    assert memory.place_for("general", rinari["id"]) == ("general", None)
    memory.remember_user("Historias de 30 s", topic="estilo", place="agent", place_ref=agent["id"])
    memory.remember_user("Formato vertical", topic="video", place="agent", place_ref=other["id"])
    memory.remember_user("El dueño vive en Costa Rica", topic="dueño")
    seen = {row["text"] for row in memory.visible_user_rows("default", chat)}
    assert seen == {"Historias de 30 s", "El dueño vive en Costa Rica"}
    assert {row["text"] for row in memory.visible_user_rows("default", rinari["id"])} == {
        "El dueño vive en Costa Rica"
    }


# -- turns of an agent ---------------------------------------------------------------


def test_an_agent_over_its_budget_does_not_start_a_turn(server) -> None:
    agent = _ok(
        _call(
            server,
            "agency.agent.create",
            {"agent": {"name": "Gastona", "budget": {"daily_tokens": 100}}},
        )
    )["agent"]
    _invoked(server, agent["session_id"], 90, 20)
    refused = _call(
        server, "session.turn.start", {"session_id": agent["session_id"], "message": "Hola"}
    )
    assert refused["ok"] is False
    assert refused["error"]["code"] == "AGENT_OVER_BUDGET"
    assert refused["error"]["details"]["agent_id"] == agent["id"]


def test_an_archived_agent_does_not_work(server) -> None:
    agent = _ok(_call(server, "agency.agent.create", {"agent": {"name": "Jubilada"}}))["agent"]
    _ok(_call(server, "agency.agent.archive", {"agent_id": agent["id"]}))
    refused = _call(
        server, "session.turn.start", {"session_id": agent["session_id"], "message": "Hola"}
    )
    assert refused["ok"] is False and refused["error"]["code"] == "AGENT_ARCHIVED"


def test_the_agents_card_is_its_own_prompt_block(server) -> None:
    from rinari.cli.agent_runtime import build_assembler_context

    services = server._services
    agent = _ok(
        _call(
            server,
            "agency.agent.create",
            {"agent": {"name": "Writer", "role": "Historias", "instructions": "Animales 3D."}},
        )
    )["agent"]
    record = services.sessions.show(agent["session_id"])
    identity = build_assembler_context(services, record).agent_identity
    assert "«Writer»" in identity and "Historias" in identity and "Animales 3D." in identity
    rinari = services.sessions.show(
        _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    )
    assert build_assembler_context(services, rinari).agent_identity is None


def test_an_agent_only_sees_its_connectors_and_its_own_accounts(server) -> None:
    from rinari.mcp.service import STATUS_CONNECTED, _ServerState

    mcp = server._services.mcp
    agency = server._services.agency
    services_rows = [{"name": name} for name in ("gmail", "telegram")]
    agency._connector_exists = lambda name: any(row["name"] == name for row in services_rows)
    mail = _ok(
        _call(server, "agency.agent.create", {"agent": {"name": "Emails", "connectors": ["gmail"]}})
    )["agent"]
    other = _ok(_call(server, "agency.agent.create", {"agent": {"name": "Otra"}}))["agent"]
    with mcp._lock:
        mcp._states["gmail"] = _ServerState(status=STATUS_CONNECTED, tools=[])
        mcp._states["telegram"] = _ServerState(status=STATUS_CONNECTED, tools=[])
        mcp._states["youtube"] = _ServerState(
            status=STATUS_CONNECTED, tools=[], owner=f"agent:{mail['id']}"
        )
    rinari = _ok(_call(server, "session.create", {"chat": True}))["session"]

    def names(session_id):
        return [row["name"] for row in mcp.connectors(None, session_id)]

    assert names(rinari["id"]) == ["gmail", "telegram"]
    assert names(mail["session_id"]) == ["gmail", "youtube"]
    assert names(other["session_id"]) == []


# -- permanent rules: a connector tool, fixed arguments, no model -------------------


def _connector(server, name, owner=""):
    repo = server._services.ctx.mcp_server_repo
    repo.add(f"mcp_{name}", name=name, transport="http", url=f"https://{name}.example/mcp")
    if owner:
        repo.set_owner(name, "global", owner)


def _run(server, task_id):
    import time

    run = _ok(_call(server, "schedule.run_now", {"task_id": task_id}))["run"]
    deadline = time.time() + 20
    while time.time() < deadline:
        current = server._services.ctx.schedule_repo.run(run["id"])
        if current["status"] not in ("running", "needs_you"):
            return current
        time.sleep(0.02)
    raise AssertionError("the rule did not finish")


def _rule(**tool):
    return {
        "name": "Correos de patito",
        "kind": "tool",
        "schedule": {"kind": "interval", "minutes": 15},
        "tool": {
            "server": "gmail",
            "tool": "search",
            "arguments": {"query": "from:x@patito.com newer_than:1d"},
            **tool,
        },
    }


@pytest.fixture
def calls(server, monkeypatch):
    seen: list[tuple[str, str, dict]] = []
    answers: dict[str, str] = {"search": "Factura 42 de x@patito.com"}

    def invoke(name, tool, arguments, project=None):
        seen.append((name, tool, arguments))
        return {"content": [{"type": "text", "text": answers.get(tool, "ok")}]}

    monkeypatch.setattr(server._services.mcp, "invoke", invoke)
    seen_answers = answers
    return seen, seen_answers


def test_a_rule_delivers_the_result_without_the_model(server, calls) -> None:
    seen, _answers = calls
    _connector(server, "gmail")
    _connector(server, "telegram")
    agent = _ok(
        _call(
            server,
            "agency.agent.create",
            {"agent": {"name": "Emails", "connectors": ["gmail", "telegram"]}},
        )
    )["agent"]
    task = _ok(
        _call(
            server,
            "schedule.create",
            {
                "task": {
                    **_rule(
                        on_result="deliver",
                        deliver={
                            "server": "telegram",
                            "tool": "send_message",
                            "arguments": {"chat": "owner", "text": "Nuevo: {result}"},
                        },
                    ),
                    "agent_id": agent["id"],
                }
            },
        )
    )["task"]
    assert task["agent_id"] == agent["id"] and task["tool"]["on_result"] == "deliver"
    run = _run(server, task["id"])
    assert run["status"] == "completed"
    assert seen == [
        ("gmail", "search", {"query": "from:x@patito.com newer_than:1d"}),
        (
            "telegram",
            "send_message",
            {"chat": "owner", "text": "Nuevo: Factura 42 de x@patito.com"},
        ),
    ]


def test_nothing_new_is_skipped_quietly(server, calls) -> None:
    _seen, answers = calls
    answers["search"] = "   "
    _connector(server, "gmail")
    task = _ok(_call(server, "schedule.create", {"task": _rule()}))["task"]
    run = _run(server, task["id"])
    assert run["status"] == "skipped" and run["reason"] == "nothing new"


def test_a_rule_stops_at_a_blocked_tool_or_out_of_its_agents_reach(
    server, calls, monkeypatch
) -> None:
    seen, _answers = calls
    _connector(server, "gmail")
    _connector(server, "youtube")
    agent = _ok(_call(server, "agency.agent.create", {"agent": {"name": "Sin correo"}}))["agent"]
    task = _ok(_call(server, "schedule.create", {"task": {**_rule(), "agent_id": agent["id"]}}))[
        "task"
    ]
    run = _run(server, task["id"])
    assert run["status"] == "blocked" and "not available" in run["reason"]
    # Its own account is in reach, even if not in its list.
    owner = f"agent:{agent['id']}"
    _ok(
        _call(
            server,
            "mcp.create",
            {
                "name": "mi-gmail",
                "transport": "http",
                "url": "https://g.example/mcp",
                "owner": owner,
            },
        )
    )
    own = _ok(
        _call(
            server,
            "schedule.create",
            {"task": {**_rule(server="mi-gmail"), "agent_id": agent["id"]}},
        )
    )["task"]
    assert _run(server, own["id"])["status"] == "completed"
    # Nobody else reaches an agent's account.
    other = _ok(_call(server, "schedule.create", {"task": _rule(server="mi-gmail")}))["task"]
    assert _run(server, other["id"])["status"] == "blocked"
    # Block in the connector's permissions wins in every case.
    monkeypatch.setattr(server._services.mcp, "decision_for", lambda name, tool: "deny")
    free = _ok(_call(server, "schedule.create", {"task": _rule()}))["task"]
    blocked = _run(server, free["id"])
    assert blocked["status"] == "blocked" and "blocked" in blocked["reason"]
    assert [call[0] for call in seen] == ["mi-gmail"]


def test_a_rule_respects_its_daily_limit(server, calls) -> None:
    _connector(server, "gmail")
    task = _ok(_call(server, "schedule.create", {"task": _rule(max_per_day=1)}))["task"]
    assert _run(server, task["id"])["status"] == "completed"
    second = _run(server, task["id"])
    assert second["status"] == "skipped" and second["reason"] == "daily limit reached"


@pytest.mark.parametrize(
    "tool",
    [
        None,
        {"server": "", "tool": "search"},
        {"server": "gmail"},
        {"server": "gmail", "tool": "search", "arguments": "from:x"},
        {"server": "gmail", "tool": "search", "on_result": "deliver"},
        {"server": "gmail", "tool": "search", "on_result": "shout"},
        {"server": "gmail", "tool": "search", "max_per_day": 0},
    ],
)
def test_rules_are_validated(server, tool) -> None:
    task = _rule()
    task["tool"] = tool
    assert _call(server, "schedule.create", {"task": task})["ok"] is False


def test_a_rule_of_an_unknown_agent_is_refused(server) -> None:
    refused = _call(server, "schedule.create", {"task": {**_rule(), "agent_id": "agent_nope"}})
    assert refused["ok"] is False


def test_an_owner_must_be_an_existing_agent(server) -> None:
    for owner in ("team:1", "agent:agent_nope", 3):
        refused = _call(
            server,
            "mcp.create",
            {"name": "x", "transport": "http", "url": "https://x.example/mcp", "owner": owner},
        )
        assert refused["ok"] is False


def test_an_agent_only_proposes_its_rules_for_the_owner_to_confirm(server) -> None:
    from types import SimpleNamespace

    from rinari.schedule.tools import ScheduleToolHost, schedule_tools

    _connector(server, "gmail")
    services = server._services
    agent = _ok(
        _call(server, "agency.agent.create", {"agent": {"name": "Emails", "connectors": ["gmail"]}})
    )["agent"]
    [tool] = schedule_tools(ScheduleToolHost(service=services.schedules, agent_id=agent["id"]))
    ctx = SimpleNamespace(session_id=agent["session_id"], origin_kind="user")
    result = tool.handler(_rule(), ctx)
    assert result.ok and result.data["status"] == "proposed"
    assert result.data["proposal"]["agent_id"] == agent["id"]
    assert result.data["proposal"]["kind"] == "tool"
    # Nothing is stored until the owner confirms the card.
    assert services.schedules.list() == []
    # Outside an agent's conversation, a connector call without the model is refused.
    [plain] = schedule_tools(ScheduleToolHost(service=services.schedules))
    refused = plain.handler(_rule(), SimpleNamespace(session_id="s", origin_kind="user"))
    assert refused.ok is False


def test_a_rule_can_wait_for_a_condition_on_the_result(server, calls) -> None:
    _seen, answers = calls
    _connector(server, "github")
    task = _ok(
        _call(
            server,
            "schedule.create",
            {"task": _rule(server="github", tool="runs", only_if_contains="FAILURE")},
        )
    )["task"]
    answers["runs"] = "ci: success on main"
    quiet = _run(server, task["id"])
    assert quiet["status"] == "skipped" and quiet["reason"] == "condition not met"
    answers["runs"] = "ci: failure on main"
    assert _run(server, task["id"])["status"] == "completed"


def _tick_run(server, task_id):
    import time

    due = server._services.schedules.get(task_id)["next_run_at"] + 1
    [run_id] = server._schedule.tick(due)
    deadline = time.time() + 20
    while time.time() < deadline:
        run = server._services.ctx.schedule_repo.run(run_id)
        if run["status"] not in ("running", "needs_you"):
            return run
        time.sleep(0.02)
    raise AssertionError("the rule did not finish")


def test_a_rule_watching_a_folder_runs_only_when_it_changes(server, calls, tmp_path) -> None:
    seen, _answers = calls
    _connector(server, "gmail")
    folder = tmp_path / "entrada"
    folder.mkdir()
    (folder / "a.txt").write_text("uno", encoding="utf-8")
    task = _ok(
        _call(server, "schedule.create", {"task": {**_rule(), "watch": {"path": str(folder)}}})
    )["task"]
    assert task["watch"] == {"path": str(folder)}
    first = _tick_run(server, task["id"])
    assert first["status"] == "skipped" and first["reason"] == "watching the folder from now"
    second = _tick_run(server, task["id"])
    assert second["status"] == "skipped" and second["reason"] == "no changes in the folder"
    (folder / "b.txt").write_text("dos", encoding="utf-8")
    assert _tick_run(server, task["id"])["status"] == "completed"
    assert len(seen) == 1
    refused = _call(
        server, "schedule.create", {"task": {**_rule(), "watch": {"path": "no/such/folder"}}}
    )
    assert refused["ok"] is False


def _rehearsal(server, task):
    import time

    started = _ok(_call(server, "schedule.rehearse", {"task": task}))
    deadline = time.time() + 20
    while time.time() < deadline:
        for evt in server.drain_events():
            if evt.get("event") == "schedule.rehearsed":
                assert evt["payload"]["job_id"] == started["job_id"]
                return evt["payload"]
        time.sleep(0.02)
    raise AssertionError("no rehearsal")


def test_a_rehearsal_says_what_the_rule_would_send_and_sends_nothing(
    server, calls, monkeypatch
) -> None:
    seen, answers = calls
    _connector(server, "gmail")
    _connector(server, "telegram")
    monkeypatch.setattr(server._services.mcp, "reads_only", lambda name, tool: tool == "search")
    rule = _rule(
        on_result="deliver",
        deliver={"server": "telegram", "tool": "send_message", "arguments": {"text": "{result}"}},
    )
    result = _rehearsal(server, rule)
    assert result["outcome"] == "would_run"
    assert result["delivery"] == {
        "server": "telegram",
        "tool": "send_message",
        "arguments": {"text": "Factura 42 de x@patito.com"},
    }
    assert [call[1] for call in seen] == ["search"]
    assert server._services.schedules.list() == []
    # A first tool that may change something is never called to rehearse.
    risky = _rehearsal(server, _rule(tool="archive"))
    assert risky["outcome"] == "not_rehearsable"
    assert [call[1] for call in seen] == ["search"]
    answers["search"] = "nada de patito"
    quiet = _rehearsal(server, _rule(only_if_contains="factura"))
    assert quiet["outcome"] == "condition_not_met"
    assert _call(server, "schedule.rehearse", {"task": {**_rule(), "kind": "agent"}})["ok"] is False


def test_a_watched_folders_fingerprint_survives_a_restart(server, calls, tmp_path) -> None:
    seen, _answers = calls
    _connector(server, "gmail")
    folder = tmp_path / "vigilada"
    folder.mkdir()
    (folder / "a.txt").write_text("uno", encoding="utf-8")
    task = _ok(
        _call(server, "schedule.create", {"task": {**_rule(), "watch": {"path": str(folder)}}})
    )["task"]
    assert _tick_run(server, task["id"])["reason"] == "watching the folder from now"
    stored = server._services.schedules.get(task["id"])["watch"]
    assert stored["path"] == str(folder) and stored["fingerprint"]
    # The Engine restarts (nothing in memory) and the folder changed meanwhile.
    server._schedule._fingerprints.clear()
    (folder / "b.txt").write_text("dos", encoding="utf-8")
    assert _tick_run(server, task["id"])["status"] == "completed"
    assert len(seen) == 1


def test_new_destinations_follow_the_agents_card_and_its_external_triggers(server) -> None:
    from rinari.cli.agent_runtime import _ask_new_destinations

    services = server._services
    show = services.sessions.show
    rinari = show(_ok(_call(server, "session.create", {"chat": True}))["session"]["id"])
    agent = _ok(_call(server, "agency.agent.create", {"agent": {"name": "Emails"}}))["agent"]
    # Nothing set: the app's setting (off by default), for Rinari and the agent.
    assert _ask_new_destinations(services, rinari) is False
    assert _ask_new_destinations(services, show(agent["session_id"])) is False
    # A run started by a rule that reads a connector is an external trigger: on.
    task = services.schedules.create({**_rule(), "agent_id": agent["id"]})
    run = _ok(_call(server, "session.create", {"chat": True}))["session"]["id"]
    services.ctx.agency_repo.set_session_agent(run, agent["id"])
    services.ctx.session_repo.set_lineage(run, None, "schedule", {"task_id": task["id"]})
    assert _ask_new_destinations(services, show(run)) is True
    # The owner turns it off on the card, and the card wins.
    _ok(
        _call(
            server,
            "agency.agent.update",
            {"agent_id": agent["id"], "patch": {"ask_new_destinations": False}},
        )
    )
    assert _ask_new_destinations(services, show(run)) is False
    _ok(
        _call(
            server,
            "agency.agent.update",
            {"agent_id": agent["id"], "patch": {"ask_new_destinations": True}},
        )
    )
    assert _ask_new_destinations(services, show(agent["session_id"])) is True
