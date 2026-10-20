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
