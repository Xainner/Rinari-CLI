"""Presencia: the one conversation with Rinari that never ends."""

from __future__ import annotations

import itertools
import json

import pytest

from rinari import presence
from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli.agent_runtime import build_assembler_context
from rinari.engine_protocol.protocol import CAPABILITIES
from rinari.engine_protocol.server import EngineServer
from rinari.prompts.assembler import PromptAssembler
from rinari.soul.store import SoulStore


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


_ids = itertools.count(1)


def _call(server, method, params=None):
    line = {"id": f"p{next(_ids)}", "method": method, "params": params or {}}
    response = server.handle_line(json.dumps(line))
    assert response is not None
    return response


def _on(server) -> dict:
    response = _call(server, "presence.set", {"enabled": True})
    assert response["ok"], response
    return response["result"]["presence"]


def test_it_is_off_until_the_owner_turns_it_on(server) -> None:
    assert CAPABILITIES["presence_v1"] is True
    assert _call(server, "presence.get")["result"]["presence"] == {
        "enabled": False,
        "session": None,
    }
    for bad in ({}, {"enabled": "yes"}, {"enabled": 1}):
        error = _call(server, "presence.set", bad)
        assert error["ok"] is False
        assert error["error"]["code"] == "INVALID_PARAMS"


def test_turning_it_on_opens_one_conversation_and_keeps_it(server) -> None:
    state = _on(server)
    session = state["session"]
    assert state["enabled"] is True
    assert session["kind"] == "CHAT"
    assert session["lineage_kind"] == "presence"
    assert session["permission_profile"] == "full-access"
    # Asking again, or turning it off and on, finds the same conversation.
    assert _call(server, "presence.get")["result"]["presence"]["session"]["id"] == session["id"]
    off = _call(server, "presence.set", {"enabled": False})["result"]["presence"]
    assert off == {"enabled": False, "session": None}
    assert _on(server)["session"]["id"] == session["id"]


def test_it_is_not_one_of_the_chats(server, tmp_path) -> None:
    chat = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})
    chat_id = chat["result"]["session"]["id"]
    presence_id = _on(server)["session"]["id"]
    listed = _call(server, "session.list", {"include_closed": True})["result"]["sessions"]
    ids = {row["id"] for row in listed}
    assert chat_id in ids
    assert presence_id not in ids


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("session.close", {"ref": "{id}"}),
        ("session.archive", {"ref": "{id}"}),
        ("session.delete", {"ref": "{id}"}),
        ("conversation.delete", {"session_id": "{id}"}),
    ],
)
def test_it_cannot_be_closed_archived_or_deleted(server, services, method, params) -> None:
    presence_id = _on(server)["session"]["id"]
    filled = {key: value.format(id=presence_id) for key, value in params.items()}
    refused = _call(server, method, filled)
    assert refused["ok"] is False
    assert refused["error"]["code"] == "CONFLICT"
    assert services.sessions.show(presence_id).state not in {"closed", "archived"}
    assert _call(server, "presence.get")["result"]["presence"]["session"]["id"] == presence_id


def test_a_conversation_lost_from_the_store_is_opened_again(server, services) -> None:
    first = _on(server)["session"]["id"]
    services.ctx.session_repo.set_lineage(first, None, "note", None)
    second = _call(server, "presence.get")["result"]["presence"]["session"]["id"]
    assert second != first
    assert services.sessions.show(second).lineage_kind == "presence"


def test_its_prompt_says_what_it_is_and_carries_the_human_register(server, services) -> None:
    record = services.sessions.show(_on(server)["session"]["id"])
    context = build_assembler_context(services, record)
    assert context.agent_identity == presence.IDENTITY
    assert context.soul.startswith("You are Rinari,")
    # The register follows the level, at the end of the system prompt.
    assert context.voice.startswith("## Character intensity: Balanced")
    assert context.voice.endswith(presence.REGISTER)
    system = PromptAssembler().build(context).system_prompt
    assert "## This conversation: Presencia" in system
    assert system.rstrip().endswith(presence.REGISTER.rstrip())


@pytest.mark.parametrize("level", ["minimal", "balanced", "full"])
def test_the_register_holds_at_every_intensity(server, services, level) -> None:
    record = services.sessions.show(_on(server)["session"]["id"])
    SoulStore(services.ctx.home).set_character_intensity(level)
    voice = build_assembler_context(services, record).voice
    assert presence.REGISTER in voice
    assert "Character intensity" in voice


def test_a_chat_does_not_get_the_register(server, services, tmp_path) -> None:
    _on(server)
    chat = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})
    record = services.sessions.show(chat["result"]["session"]["id"])
    context = build_assembler_context(services, record)
    assert context.agent_identity is None
    assert "How you talk here" not in context.voice
    assert "time_of_day" not in context.environment


def test_it_knows_the_part_of_the_day(server, services) -> None:
    record = services.sessions.show(_on(server)["session"]["id"])
    environment = build_assembler_context(services, record).environment
    assert environment["time_of_day"] in {"night", "morning", "afternoon", "evening"}
