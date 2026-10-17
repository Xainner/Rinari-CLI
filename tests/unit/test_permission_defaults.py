"""Where the permission mode comes from (plan permisos v4 P-5).

A conversation keeps its own mode; a new one starts in its project's default
(Proyecto without one), and a scheduled task can run in a mode of its own.
"""

from __future__ import annotations

import itertools
import json

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.server import EngineServer
from rinari.schedule.service import ScheduledTaskError


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


_IDS = itertools.count()


def _call(server, method, params=None):
    line = {"id": f"perm-{next(_IDS)}", "method": method, "params": params or {}}
    response = server.handle_line(json.dumps(line))
    assert response is not None
    return response


def _ok(response):
    assert response["ok"] is True, response
    return response["result"]


def _project(server, tmp_path, name="web"):
    folder = tmp_path / name
    folder.mkdir()
    created = _ok(
        _call(
            server,
            "project.create",
            {"name": name, "folders": [{"path": str(folder), "trust": True}]},
        )
    )
    return created["project"]


def test_a_new_conversation_starts_in_the_projects_default(server, tmp_path) -> None:
    project = _project(server, tmp_path)
    assert project["permission_profile"] is None
    plain = _ok(_call(server, "session.create", {"project_id": project["id"]}))["session"]
    assert plain["permission_profile"] == "workspace"

    updated = _ok(
        _call(
            server,
            "project.update",
            {"project_id": project["id"], "permission_profile": "read-only"},
        )
    )["project"]
    assert updated["permission_profile"] == "read-only"
    reader = _ok(_call(server, "session.create", {"project_id": project["id"]}))["session"]
    assert reader["permission_profile"] == "read-only"
    # Asking for a mode wins over the default, and older conversations keep theirs.
    full = _ok(
        _call(
            server,
            "session.create",
            {"project_id": project["id"], "permission_profile": "full-access"},
        )
    )["session"]
    assert full["permission_profile"] == "full-access"
    assert server._services.sessions.show(plain["id"]).permission_profile == "workspace"

    cleared = _ok(
        _call(server, "project.update", {"project_id": project["id"], "permission_profile": ""})
    )["project"]
    assert cleared["permission_profile"] is None
    again = _ok(_call(server, "session.create", {"project_id": project["id"]}))["session"]
    assert again["permission_profile"] == "workspace"


def test_an_unknown_default_is_refused(server, tmp_path) -> None:
    project = _project(server, tmp_path)
    response = _call(
        server, "project.update", {"project_id": project["id"], "permission_profile": "yolo"}
    )
    assert response["ok"] is False


def test_a_conversation_opened_by_folder_follows_the_project(server, services, tmp_path) -> None:
    project = _project(server, tmp_path)
    services.projects.update(project["id"], permission_profile="full-access")
    record = services.sessions.new(cwd=tmp_path / "web", mode="build")
    assert record.project_id == project["id"]
    assert record.permission_profile == "full-access"


def test_a_scheduled_task_keeps_its_mode_and_validates_it(services, tmp_path) -> None:
    task = services.schedules.create(
        {
            "name": "Revisión",
            "kind": "agent",
            "schedule": {"kind": "daily", "time": "08:30"},
            "prompt": "Revisa el repo.",
            "permission_profile": "read-only",
        }
    )
    assert services.schedules.get(task["id"])["permission_profile"] == "read-only"
    # Without one, the project's default decides when it runs.
    same = services.schedules.update(task["id"], {"permission_profile": None})
    assert same["permission_profile"] is None
    with pytest.raises(ScheduledTaskError):
        services.schedules.update(task["id"], {"permission_profile": "yolo"})


def test_a_scheduled_run_starts_in_the_task_mode_or_the_projects(server, tmp_path) -> None:
    project = _project(server, tmp_path)
    _ok(
        _call(
            server,
            "project.update",
            {"project_id": project["id"], "permission_profile": "read-only"},
        )
    )
    schedule = server._schedule
    base = {
        "name": "Revisión",
        "kind": "agent",
        "schedule": {"kind": "daily", "time": "08:30"},
        "prompt": "Revisa el repo.",
        "project_id": project["id"],
    }
    inherits = server._services.schedules.create(base)
    own = server._services.schedules.create({**base, "permission_profile": "full-access"})
    started: list[str] = []
    schedule._turns.start_turn = lambda session_id, *a, **k: (  # type: ignore[method-assign]
        started.append(session_id) or {"turn_id": "trn_x"}
    )
    for task in (inherits, own):
        schedule._start_agent_run(server._services.schedules.get(task["id"]), "run_1", 0.0)
    modes = [server._services.sessions.show(sid).permission_profile for sid in started]
    assert modes == ["read-only", "full-access"]
