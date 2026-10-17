"""Memory places (plan 0.4.0 #75) and «Sobre ti» from the welcome (#71)."""

from __future__ import annotations

import pytest

from rinari.memory.service import MemoryService
from tests.unit.test_learned_memory import SESSION, _session, _tool_ctx


def test_general_profile_and_chat_records_reach_only_their_prompt(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    memory.remember_user("Prefers short answers", kind="preference", topic="tone")
    memory.remember_user(
        "As a designer, review contrast first",
        kind="rule",
        topic="review",
        place="profile",
        place_ref="designer",
    )
    memory.remember_user(
        "This logo uses the teal palette",
        kind="decision",
        topic="palette",
        place="chat",
        place_ref="ses_logo",
    )
    plain = memory.prompt_segment(None, "logo", profile_id="default", session_id="ses_other")
    assert plain is not None and "short answers" in plain
    assert "contrast" not in plain and "teal" not in plain
    full = memory.prompt_segment(None, "logo", profile_id="designer", session_id="ses_logo")
    assert full is not None
    assert "[profile:rule] As a designer" in full and "[chat:decision] This logo" in full


def test_the_same_topic_in_two_chats_is_two_records(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    for chat in ("ses_a", "ses_b"):
        memory.remember_user(
            f"Draft lives in {chat}", kind="fact", topic="draft", place="chat", place_ref=chat
        )
    assert len(memory.list_user()) == 2


def test_a_chats_memory_goes_with_the_chat(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    _session(app_ctx, "ses_gone")
    memory.remember_user("Only here", kind="fact", topic="x", place="chat", place_ref="ses_gone")
    memory.remember_user("Everywhere", kind="fact", topic="y")
    memory.delete_conversation("ses_gone")
    assert [row["text"] for row in memory.list_user()] == ["Everywhere"]


def test_move_between_places_and_into_a_project(app_ctx, tmp_path) -> None:
    memory = MemoryService(app_ctx)
    result = memory.remember_user("Uses the MaestroX guide", kind="resource", topic="guide")
    moved = memory.move(result["id"], "profile", place_ref="writer")
    assert moved["place"] == "profile" and moved["place_ref"] == "writer"
    project = str(tmp_path / "maestro")
    in_project = memory.move(result["id"], "project", project_root=project)
    assert in_project["scope"] == "project" and in_project["kind"] == "resource"
    assert memory.list_user() == []
    back = memory.move(in_project["id"], "general")
    assert back["scope"] == "user" and back["place"] == "general"
    with pytest.raises(Exception, match="needs its chat"):
        memory.move(back["id"], "chat")


def test_the_model_chooses_chat_and_the_owner_changes_it_on_the_card(app_ctx, tmp_path) -> None:
    memory, ctx, _ = _tool_ctx(app_ctx, tmp_path)
    proposed = _propose_chat(ctx)
    assert proposed.ok and proposed.data["status"] == "pending"
    [candidate] = memory.list_candidates()
    assert candidate["place"] == "chat" and candidate["place_ref"] == SESSION
    approved = memory.resolve_candidate(candidate["id"], "allow_once", place="general")
    [row] = memory.list_user()
    assert approved["status"] == "accepted" and row["place"] == "general"


def _propose_chat(ctx):
    from rinari.tools.native.memory import memory_propose

    return memory_propose(
        {
            "topic": "palette",
            "text": "This chat's logo is teal",
            "kind": "decision",
            "scope": "chat",
        },
        ctx,
    )


def test_about_me_replaces_and_always_opens_the_prompt(app_ctx) -> None:
    memory = MemoryService(app_ctx)
    memory.remember_user("Works on Windows", kind="environment", topic="os")
    about = memory.about_set(name="Xai", uses=["code", "Videos"], preferences="Short answers")
    assert about["name"] == "Xai" and about["uses"] == ["code", "Videos"]
    prompt = memory.prompt_segment(None, "unrelated words")
    assert prompt is not None
    assert prompt.index("About the owner") < prompt.index("Works on Windows")
    assert "Call them: Xai" in prompt and "Main uses: code; Videos" in prompt
    again = memory.about_set(name="Xainner", uses=[], preferences=None)
    assert again == {**again, "name": "Xainner", "uses": [], "preferences": None}
    assert len([row for row in memory.list_user() if row["topic"].startswith("about:")]) == 1


def test_protocol_about_move_and_place_labels(app_ctx, tmp_path) -> None:
    from rinari.application.provider_service import AddProviderInput
    from rinari.application.services import build_services
    from rinari.engine_protocol.server import EngineServer
    from tests.unit.test_learned_memory import _call

    user_home = tmp_path / "home"
    user_home.mkdir()
    services = build_services(app_ctx, user_home=user_home)
    services.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    server = EngineServer(services)
    try:
        saved = _call(
            server, "memory.about.set", {"name": "Xai", "uses": ["code"], "preferences": ""}
        )
        assert saved["ok"], saved
        assert _call(server, "memory.about.get")["result"]["name"] == "Xai"
        result = services.memory.remember_user("Teal palette", kind="decision", topic="palette")
        moved = _call(
            server, "memory.move", {"id": result["id"], "place": "profile", "place_ref": "default"}
        )
        assert moved["ok"], moved
        assert moved["result"]["record"]["place"] == "profile"
        assert moved["result"]["record"]["place_label"]
        listed = _call(server, "memory.list", {"scope": "all"})["result"]["records"]
        assert {row["place"] for row in listed} == {"general", "profile"}
        bad = _call(server, "memory.move", {"id": result["id"], "place": "somewhere"})
        assert bad["error"]["code"] == "INVALID_PARAMS"
    finally:
        server.close()


def test_a_memory_says_where_it_came_from_and_when_it_was_last_used(app_ctx) -> None:
    """#75: the manager shows the origin conversation and what has not been used."""
    from rinari.memory.service import LEARNED_PROVENANCE_PREFIX

    memory = MemoryService(app_ctx)
    _session(app_ctx, "ses_origin")
    saved = memory.remember_user(
        "Deploys go through Saturno",
        kind="environment",
        topic="deploy",
        origin_session_id="ses_origin",
    )
    learned = memory.remember_user(
        "Old learned fact",
        kind="fact",
        topic="old",
        provenance=f"{LEARNED_PROVENANCE_PREFIX}session/ses_origin",
    )
    row = memory.get_user(saved["id"])
    assert row["source_session_id"] == "ses_origin" and row["last_used_at"] is None
    assert memory.get_user(learned["id"])["source_session_id"] is None

    # Going into a prompt counts as a use.
    assert "Saturno" in (memory.prompt_segment(None, "deploy") or "")
    assert memory.get_user(saved["id"])["last_used_at"] is not None


def test_the_list_names_the_origin_conversation(app_ctx, tmp_path) -> None:
    import json

    from rinari.application.services import build_services
    from rinari.engine_protocol.server import EngineServer
    from rinari.memory.service import LEARNED_PROVENANCE_PREFIX

    services = build_services(app_ctx, user_home=tmp_path)
    _session(app_ctx, "ses_origin")
    services.memory.remember_user(
        "Saved here", kind="fact", topic="a", origin_session_id="ses_origin"
    )
    services.memory.remember_user(
        "Learned before 0.4.0",
        kind="fact",
        topic="b",
        provenance=f"{LEARNED_PROVENANCE_PREFIX}session/ses_origin",
    )
    services.memory.remember_user("No origin", kind="fact", topic="c")
    server = EngineServer(services, user_home=tmp_path)
    try:
        response = server.handle_line(
            json.dumps({"id": "l", "method": "memory.list", "params": {"scope": "user"}})
        )
    finally:
        server.close()
    origins = {row["text"]: row["origin"] for row in response["result"]["records"]}
    assert origins["Saved here"] == {"session_id": "ses_origin", "title": "t", "exists": True}
    assert origins["Learned before 0.4.0"]["session_id"] == "ses_origin"
    assert origins["No origin"] is None
