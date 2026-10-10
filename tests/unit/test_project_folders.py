"""Projects with several working folders: validation, creation, policy, protocol."""

from __future__ import annotations

import dataclasses
import json
from itertools import count
from pathlib import Path

import pytest

from rinari.application.project_service import ProjectFolderError
from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli.agent_runtime import _extra_roots
from rinari.engine_protocol.server import EngineServer
from rinari.policy.engine import (
    CAPABILITY_FS_WRITE,
    CAPABILITY_SHELL,
    PolicyAction,
    PolicyEngine,
    SessionScope,
    normalize_profile,
)
from rinari.shared.errors import ConflictError


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


def _dir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return path


# -- validation ---------------------------------------------------------------------


def test_each_problem_has_its_code(services, tmp_path):
    taken = services.projects.upsert(_dir(tmp_path, "taken"))
    afile = tmp_path / "file.txt"
    afile.write_text("x")
    checks = services.projects.validate_folders(
        [
            str(_dir(tmp_path, "web")),
            str(tmp_path / "missing"),
            str(afile),
            str(tmp_path / "web"),
            str(_dir(tmp_path, "web") / "nested"),
            taken.canonical_root,
            str(services.ctx.layout.root),
        ]
    )
    (tmp_path / "web" / "nested").mkdir(exist_ok=True)
    codes = [None if c.ok else c.code for c in checks]
    assert codes == [
        None,
        "NOT_FOUND",
        "NOT_DIRECTORY",
        "DUPLICATE",
        "NOT_FOUND",
        "IN_PROJECT",
        "ENGINE_HOME",
    ]
    in_project = checks[5].as_dict()["error"]
    assert in_project["project_id"] == taken.id and in_project["project_name"] == "taken"
    nested = services.projects.validate_folders(
        [str(tmp_path / "web"), str(tmp_path / "web" / "nested")]
    )
    assert [c.code for c in nested] == [None, "NESTED"]


def test_creation_is_all_or_nothing(services, tmp_path):
    with pytest.raises(ProjectFolderError) as exc:
        services.projects.create(
            name="Mal", folders=[str(_dir(tmp_path, "a")), str(tmp_path / "nope")]
        )
    assert [c.code for c in exc.value.checks if not c.ok] == ["NOT_FOUND"]
    assert services.projects.list() == []


# -- folders belong to their project -----------------------------------------------------


def test_create_with_extra_folders_and_find_them(services, tmp_path):
    api, web = _dir(tmp_path, "api"), _dir(tmp_path, "web")
    project = services.projects.create(
        name="Tienda", description="La tienda", folders=[str(api), str(web)]
    )
    folders = services.projects.folders.list(project.id)
    assert [(f["path"], f["primary"]) for f in folders] == [
        (str(api.resolve()), True),
        (str(web.resolve()), False),
    ]
    # Opening an extra folder opens its project, never a new one.
    assert services.projects.upsert(web).id == project.id
    assert services.projects.folders.owner_of(web / "src" / "app.ts") == project.id
    # A session started inside the extra folder binds to the project.
    record = services.sessions.new(web, mode="build")
    assert record.project_id == project.id
    assert record.project_root_snapshot == project.canonical_root
    assert record.current_cwd == str(web.resolve())


def test_existing_projects_have_their_root_as_primary(services, tmp_path):
    project = services.projects.upsert(_dir(tmp_path, "old"))
    assert services.projects.folders.list(project.id) == [
        {"path": project.canonical_root, "primary": True, "position": 0, "label": ""}
    ]


def test_remove_extra_but_never_the_primary(services, tmp_path):
    api, web = _dir(tmp_path, "api"), _dir(tmp_path, "web")
    project = services.projects.create(name="Tienda", folders=[str(api)])
    services.projects.add_folder(project.id, str(web))
    with pytest.raises(ProjectFolderError):
        services.projects.add_folder(project.id, str(web))
    with pytest.raises(ConflictError):
        services.projects.remove_folder(project.id, str(api))
    services.projects.remove_folder(project.id, str(web))
    assert len(services.projects.folders.list(project.id)) == 1


# -- policy: trusted extra folders are the project -------------------------------------------


def _scope(root: Path, extras=(), profile="workspace", cwd=None) -> SessionScope:
    return SessionScope(
        kind="PROJECT",
        root=root,
        cwd=cwd or root,
        profile=normalize_profile(profile),
        user_home=Path("/home/x"),
        extra_roots=tuple(extras),
    )


def test_policy_writes_and_commands_in_extra_folders(tmp_path):
    root, web, other = tmp_path / "api", tmp_path / "web", tmp_path / "other"
    engine = PolicyEngine()
    with_extra = _scope(root, [web])
    assert engine.decide(CAPABILITY_FS_WRITE, with_extra, path=str(web / "a.ts")).action is (
        PolicyAction.ALLOW
    )
    assert engine.decide(CAPABILITY_FS_WRITE, _scope(root), path=str(web / "a.ts")).action is (
        PolicyAction.ASK
    )
    assert engine.decide(CAPABILITY_FS_WRITE, with_extra, path=str(other / "a.ts")).action is (
        PolicyAction.ASK
    )
    in_web = dataclasses.replace(with_extra, command_cwd=web)
    assert (
        engine.decide(CAPABILITY_SHELL, in_web, command="npm install").action is PolicyAction.ALLOW
    )
    delete = engine.decide(CAPABILITY_SHELL, in_web, command="rm -r ./build")
    assert delete.action is PolicyAction.ALLOW
    outside = engine.decide(CAPABILITY_SHELL, with_extra, command=f"rm -r {other / 'x'}")
    assert outside.rule_id == "delete_outside_root"


def test_only_trusted_present_folders_are_working_folders(services, tmp_path):
    api, web, docs = _dir(tmp_path, "api"), _dir(tmp_path, "web"), _dir(tmp_path, "docs")
    services.projects.create(name="Tienda", folders=[str(api), str(web), str(docs)])
    services.trust.add(web)
    record = services.sessions.new(api, mode="build")
    assert _extra_roots(services, record) == (web.resolve(),)


# -- protocol -------------------------------------------------------------------------------


@pytest.fixture
def server(services, tmp_path):
    engine = EngineServer(services, user_home=tmp_path / "home")
    yield engine
    engine.close()


_ids = count()


def _call(server, method, params=None):
    response = server.handle_line(
        json.dumps({"id": f"r{next(_ids)}", "method": method, "params": params or {}})
    )
    assert response is not None
    return response


def test_protocol_create_validate_and_manage_folders(server, services, tmp_path):
    api, web, extra = _dir(tmp_path, "api"), _dir(tmp_path, "web"), _dir(tmp_path, "extra")
    report = _call(server, "project.folders.validate", {"paths": [str(api), str(api)]})["result"]
    assert [f["ok"] for f in report["folders"]] == [True, False]
    assert report["folders"][0]["trust_state"] in {"not-trusted", "not-found"}
    created = _call(
        server,
        "project.create",
        {
            "name": "Tienda",
            "description": "La tienda",
            "folders": [{"path": str(api), "trust": True}, {"path": str(web), "trust": True}],
        },
    )["result"]
    project = created["project"]
    assert project["name"] == "Tienda" and project["description"] == "La tienda"
    assert [f["primary"] for f in project["folders"]] == [True, False]
    assert {t["state"] for t in created["trust"]} == {"trusted"}
    assert created["session"]["project_id"] == project["id"]

    bad = _call(server, "project.create", {"name": "Otra", "folders": [{"path": str(web)}]})
    assert bad["ok"] is False
    assert bad["error"]["details"]["folders"][0]["error"]["code"] == "IN_PROJECT"

    added = _call(server, "project.folder.add", {"project_id": project["id"], "path": str(extra)})[
        "result"
    ]["project"]
    assert len(added["folders"]) == 3
    primary = _call(
        server, "project.folder.remove", {"project_id": project["id"], "path": str(api)}
    )
    assert primary["ok"] is False and primary["error"]["code"] == "CONFLICT"
    removed = _call(
        server, "project.folder.remove", {"project_id": project["id"], "path": str(extra)}
    )["result"]["project"]
    assert len(removed["folders"]) == 2
