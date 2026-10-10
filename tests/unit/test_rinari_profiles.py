"""Rinari profiles as workspaces: the active one owns new work, moves keep invariants."""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.server import EngineServer
from rinari.shared.errors import ConflictError, InvalidUsageError, NotFoundError


@pytest.fixture
def services(app_ctx, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    container = build_services(app_ctx, user_home=home)
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
def profiles(services):
    return services.rinari_profiles


def _folder(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    # A strong marker: the folder is a project root on its own.
    (path / ".rinari").mkdir(exist_ok=True)
    (path / ".rinari" / "project.toml").write_text("", encoding="utf-8")
    return path


# -- store and active profile --------------------------------------------------------


def test_the_default_profile_always_exists_and_cannot_be_removed(profiles):
    listed = profiles.store.list()
    assert listed[0].id == "default" and listed[0].builtin
    assert profiles.active_id() == "default"
    with pytest.raises(ConflictError):
        profiles.store.remove("default")
    edited = profiles.store.update("default", name="Casa")
    assert edited.name == "Casa" and profiles.store.get("default").name == "Casa"
    assert [b.id for b in profiles.store.list()] == ["default"]


def test_activation_moves_only_the_pointer(profiles):
    profiles.store.create("trabajo", name="Trabajo")
    previous, bundle = profiles.activate("trabajo")
    assert (previous, bundle.id, profiles.active_id()) == ("default", "trabajo", "trabajo")
    with pytest.raises(NotFoundError):
        profiles.activate("nope")
    # A pointer to a profile whose file is gone falls back to the default.
    profiles.store.remove("trabajo")
    assert profiles.active_id() == "default"


def test_update_validates_and_clears_options(profiles):
    profiles.store.create("p1", name="Uno", mode="plan", soul_id="s1")
    updated = profiles.store.update("p1", mode=None, soul_id=None, description="nuevo")
    assert (updated.mode, updated.soul_id, updated.description) == (None, None, "nuevo")
    with pytest.raises(InvalidUsageError):
        profiles.store.update("p1", name="")


# -- new work belongs to the active profile ----------------------------------------------


def test_new_projects_and_conversations_take_the_active_profile(services, profiles, tmp_path):
    before = services.sessions.new(tmp_path, forced_chat=True)
    assert before.rinari_profile_id == "default"
    profiles.store.create("trabajo", name="Trabajo")
    profiles.activate("trabajo")
    chat = services.sessions.new(tmp_path, forced_chat=True)
    assert chat.rinari_profile_id == "trabajo"
    project_session = services.sessions.new(_folder(tmp_path, "repo"))
    project = services.projects.get(project_session.project_id)
    assert project.rinari_profile_id == "trabajo"
    assert project_session.rinari_profile_id == "trabajo"


def test_a_project_conversation_always_has_the_projects_profile(services, profiles, tmp_path):
    repo = _folder(tmp_path, "repo")
    first = services.sessions.new(repo)  # project born in default
    profiles.store.create("otro", name="Otro")
    profiles.activate("otro")
    second = services.sessions.new(repo)
    assert second.project_id == first.project_id
    assert second.rinari_profile_id == "default"
    with pytest.raises(InvalidUsageError):
        services.sessions.new(repo, rinari_profile_id="otro")


def test_a_new_conversation_takes_its_profiles_soul_and_mode(services, profiles, tmp_path):
    profiles.store.create("seria", name="Seria", soul_id="profesional", mode="plan")
    profiles.activate("seria")
    record = services.sessions.new(tmp_path, forced_chat=True)
    assert (record.soul_id, record.mode) == ("profesional", "plan")
    explicit = services.sessions.new(tmp_path, forced_chat=True, mode="review")
    assert explicit.mode == "review"


def test_forks_keep_profile_and_soul(services, profiles, tmp_path):
    profiles.store.create("seria", name="Seria", soul_id="profesional")
    profiles.activate("seria")
    source = services.sessions.new(tmp_path, forced_chat=True)
    profiles.activate("default")
    fork = services.sessions.fork(source.id).session
    assert (fork.rinari_profile_id, fork.soul_id) == ("seria", "profesional")


# -- moving work ----------------------------------------------------------------------------


def test_moving_a_project_takes_its_conversations(services, profiles, tmp_path):
    repo = _folder(tmp_path, "repo")
    a = services.sessions.new(repo)
    b = services.sessions.new(repo)
    loose = services.sessions.new(tmp_path, forced_chat=True)
    profiles.store.create("trabajo", name="Trabajo")
    moved = profiles.move_project(a.project_id, "trabajo")
    assert set(moved.session_ids) == {a.id, b.id}
    assert services.projects.get(a.project_id).rinari_profile_id == "trabajo"
    assert {services.sessions.show(x).rinari_profile_id for x in (a.id, b.id)} == {"trabajo"}
    assert services.sessions.show(loose.id).rinari_profile_id == "default"


def test_a_project_conversation_cannot_move_alone(services, profiles, tmp_path):
    record = services.sessions.new(_folder(tmp_path, "repo"))
    profiles.store.create("trabajo", name="Trabajo")
    with pytest.raises(InvalidUsageError):
        profiles.move_session(record.id, "trabajo")
    loose = services.sessions.new(tmp_path, forced_chat=True)
    assert profiles.move_session(loose.id, "trabajo").session_ids == (loose.id,)


def test_moving_a_conversation_into_a_project_adopts_its_profile(services, profiles, tmp_path):
    profiles.store.create("trabajo", name="Trabajo")
    profiles.activate("trabajo")
    project = services.sessions.new(_folder(tmp_path, "repo"))
    profiles.activate("default")
    loose = services.sessions.new(tmp_path, forced_chat=True)
    moved = services.sessions.move(loose.id, project.project_id)
    assert moved.rinari_profile_id == "trabajo"
    assert services.sessions.show(loose.id).rinari_profile_id == "trabajo"


def test_removing_a_profile_reassigns_its_work(services, profiles, tmp_path):
    profiles.store.create("trabajo", name="Trabajo")
    profiles.activate("trabajo")
    record = services.sessions.new(_folder(tmp_path, "repo"))
    result = profiles.remove("trabajo")
    assert result["reassigned"] == {"to": "default", "projects": 1, "sessions": 1}
    assert result["active_id"] == "default"
    assert services.sessions.show(record.id).rinari_profile_id == "default"


def test_orphaned_work_goes_back_to_the_default(services, profiles, tmp_path):
    record = services.sessions.new(tmp_path, forced_chat=True)
    services.ctx.session_repo.set_rinari_profile([record.id], "deleted-by-hand")
    assert profiles.reconcile_orphans() == 1
    assert services.sessions.show(record.id).rinari_profile_id == "default"


# -- protocol --------------------------------------------------------------------------


@pytest.fixture
def server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


_ids = count()


def _call(server, method, params=None):
    line = {"id": f"r{next(_ids)}", "method": method, "params": params or {}}
    response = server.handle_line(json.dumps(line))
    assert response is not None
    return response


def _ok(response):
    assert response["ok"], response
    return response["result"]


def test_protocol_switch_lists_and_moves(server, tmp_path):
    _ok(_call(server, "profile_bundle.create", {"id": "trabajo", "name": "Trabajo"}))
    listed = _ok(_call(server, "profile_bundle.list"))
    assert listed["active_id"] == "default"
    assert [(p["id"], p["active"], p["builtin"]) for p in listed["profiles"]] == [
        ("default", True, True),
        ("trabajo", False, False),
    ]
    home_chat = _ok(_call(server, "session.create", {"chat": True}))["session"]
    assert home_chat["rinari_profile_id"] == "default"
    server.drain_events()
    switched = _ok(_call(server, "profile_bundle.activate", {"id": "trabajo"}))
    assert (switched["active_id"], switched["previous_id"]) == ("trabajo", "default")
    assert [e["event"] for e in server.drain_events()] == ["profile_bundle.activated"]
    work_chat = _ok(_call(server, "session.create", {"chat": True}))["session"]
    assert work_chat["rinari_profile_id"] == "trabajo" and work_chat["mode"] == "build"
    active_ids = [
        s["id"]
        for s in _ok(_call(server, "session.list", {"rinari_profile_id": "active"}))["sessions"]
    ]
    assert active_ids == [work_chat["id"]]
    every = {s["id"] for s in _ok(_call(server, "session.list"))["sessions"]}
    assert {home_chat["id"], work_chat["id"]} <= every
    count_row = next(
        p for p in _ok(_call(server, "profile_bundle.list"))["profiles"] if p["id"] == "trabajo"
    )
    assert count_row["counts"] == {"projects": 0, "sessions": 1}

    moved = _ok(
        _call(
            server,
            "session.move_profile",
            {"session_id": home_chat["id"], "rinari_profile_id": "trabajo"},
        )
    )
    assert moved["session"]["rinari_profile_id"] == "trabajo"


def test_protocol_a_project_conversation_needs_a_policy(server, tmp_path):
    repo = _folder(tmp_path, "repo")
    opened = _ok(_call(server, "project.open", {"path": str(repo)}))
    session_id = opened["session"]["id"]
    _ok(_call(server, "profile_bundle.create", {"id": "trabajo", "name": "Trabajo"}))
    refused = _call(
        server, "session.move_profile", {"session_id": session_id, "rinari_profile_id": "trabajo"}
    )
    assert refused["ok"] is False
    details = refused["error"]["details"]
    assert details["code"] == "PROJECT_POLICY_REQUIRED"
    assert details["project_id"] == opened["project"]["id"]
    assert details["project_session_count"] == 1

    whole = _ok(
        _call(
            server,
            "session.move_profile",
            {
                "session_id": session_id,
                "rinari_profile_id": "trabajo",
                "project_policy": "move_project",
            },
        )
    )
    assert whole["project"]["rinari_profile_id"] == "trabajo"
    assert whole["session_ids"] == [session_id]
    recents = _ok(_call(server, "project.list_recent", {"rinari_profile_id": "trabajo"}))
    assert [p["id"] for p in recents["projects"]] == [opened["project"]["id"]]
    assert _ok(_call(server, "project.list_recent", {"rinari_profile_id": "default"})) == {
        "projects": []
    }

    out = _ok(
        _call(
            server,
            "session.move_profile",
            {
                "session_id": session_id,
                "rinari_profile_id": "default",
                "project_policy": "leave_project",
            },
        )
    )
    assert out["session"]["project_id"] is None
    assert out["session"]["rinari_profile_id"] == "default"


def test_protocol_remove_and_unknown_profiles(server):
    _ok(_call(server, "profile_bundle.create", {"id": "temporal", "name": "Temporal"}))
    _ok(_call(server, "profile_bundle.activate", {"id": "temporal"}))
    _ok(_call(server, "session.create", {"chat": True}))
    removed = _ok(_call(server, "profile_bundle.remove", {"id": "temporal"}))
    assert removed["reassigned"]["sessions"] == 1 and removed["active_id"] == "default"
    assert _call(server, "profile_bundle.remove", {"id": "default"})["ok"] is False
    bad = _call(server, "session.create", {"chat": True, "rinari_profile_id": "nope"})
    assert bad["ok"] is False and bad["error"]["code"] == "INVALID_PARAMS"
    updated = _ok(_call(server, "profile_bundle.update", {"id": "default", "name": "Casa"}))
    assert updated["profile"]["name"] == "Casa"
