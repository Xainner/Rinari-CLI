"""Engine project workspace: list_recent/open/status (docs/desktop 01, P1).

Recency derives from the shared session table (no second store), so CLI
and desktop activity both count. Opening reuses the bound session (touching
its activity) or creates + promotes one; $HOME is never an openable root.
"""

from __future__ import annotations

import json
import subprocess
from itertools import count
from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol import workspace
from rinari.engine_protocol.server import EngineServer
from rinari.projects._git_process import GitCommandResult


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


def _req(request_id, method, params=None):
    line: dict = {"id": request_id, "method": method}
    if params is not None:
        line["params"] = params
    return json.dumps(line)


def _ok(response):
    assert response is not None and response["ok"] is True, response
    return response["result"]


def _err(response):
    assert response is not None and response["ok"] is False, response
    return response["error"]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def _git_repo(tmp_path, name="repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    (root / "f.py").write_text("x = 1\n")
    _git(root, "add", "f.py")
    _git(root, "commit", "-qm", "seed")
    return root


def _open(server, tag, path):
    return _ok(server.handle_line(_req(tag, "project.open", {"path": str(path)})))


def _recent_roots(server):
    result = _ok(server.handle_line(_req(f"r-{next(_rids)}", "project.list_recent", {})))
    return [p["root"] for p in result["projects"]]


_rids = count()


def test_list_recent_empty(server) -> None:
    assert _ok(server.handle_line(_req("r", "project.list_recent", {}))) == {"projects": []}


def test_open_creates_project_and_promotes_session(server, tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    first = _open(server, "o1", plain)
    assert first["created"] is True
    assert first["project"]["root"] == str(plain.resolve())
    assert first["session"]["kind"] == "PROJECT"
    assert first["session"]["project_root"] == str(plain.resolve())

    second = _open(server, "o2", plain)
    assert second["created"] is False
    assert second["session"]["id"] == first["session"]["id"]
    assert second["project"]["id"] == first["project"]["id"]


def test_open_reorders_recents(server, tmp_path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    _open(server, "o1", a)
    _open(server, "o2", b)
    assert _recent_roots(server) == [str(b.resolve()), str(a.resolve())]
    _open(server, "o3", a)
    assert _recent_roots(server) == [str(a.resolve()), str(b.resolve())]


def test_list_recent_carries_binding_and_limit_validation(server, tmp_path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    opened = _open(server, "o1", a)
    recents = _ok(server.handle_line(_req("r2", "project.list_recent", {"limit": 20})))["projects"]
    assert len(recents) == 1
    entry = recents[0]
    assert entry["id"] == opened["project"]["id"]
    assert entry["active_session_id"] == opened["session"]["id"]
    assert isinstance(entry["last_opened_at"], str) and entry["last_opened_at"]
    assert (
        _err(server.handle_line(_req("r3", "project.list_recent", {"limit": 0})))["code"]
        == "INVALID_PARAMS"
    )


def test_open_rejects_missing_file_and_home(services, server, tmp_path) -> None:
    assert (
        _err(server.handle_line(_req("o1", "project.open", {"path": str(tmp_path / "nope")})))[
            "code"
        ]
        == "INVALID_PARAMS"
    )
    afile = tmp_path / "f"
    afile.write_text("x")
    assert (
        _err(server.handle_line(_req("o2", "project.open", {"path": str(afile)})))["code"]
        == "INVALID_PARAMS"
    )
    home = Path(services.ctx.home)
    home.mkdir(parents=True, exist_ok=True)
    assert (
        _err(server.handle_line(_req("o3", "project.open", {"path": str(home)})))["code"]
        == "PERMISSION_DENIED"
    )


def test_status_plain_dir_has_no_git_and_no_binding(server, tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    status = _ok(server.handle_line(_req("s", "project.status", {"path": str(plain)})))
    assert status["project"] == {"root": str(plain.resolve())}
    assert status["status"]["available"] is False
    assert status["active_session_id"] is None


def test_status_git_repo_matches_git_truth_with_binding(server, tmp_path) -> None:
    repo = _git_repo(tmp_path)
    before = _ok(server.handle_line(_req("s1", "project.status", {"path": str(repo)})))
    assert before["status"]["available"] is True
    assert isinstance(before["status"]["branch"], str) and before["status"]["branch"]
    assert isinstance(before["status"]["head"], str) and before["status"]["head"]
    assert before["status"]["dirty"] is False
    assert before["active_session_id"] is None

    (repo / "new.py").write_text("y = 2\n")
    opened = _open(server, "o", repo)
    assert opened["session"]["kind"] == "PROJECT"
    after = _ok(server.handle_line(_req("s2", "project.status", {"path": str(repo)})))
    assert after["status"]["dirty"] is True
    assert {f["path"] if isinstance(f, dict) else f for f in after["status"]["files"]}
    assert after["active_session_id"] == opened["session"]["id"]


def test_status_reports_structured_git_timeout(server, tmp_path, monkeypatch) -> None:
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(
        workspace,
        "run_git",
        lambda *args, **kwargs: GitCommandResult("", None, timed_out=True, error="slow git"),
    )
    result = _ok(server.handle_line(_req("timeout", "project.status", {"path": str(repo)})))
    assert result["exists"] is True
    assert result["stale"] is True
    assert result["git"]["error"] == {
        "code": "GIT_TIMEOUT",
        "message": "slow git",
        "retryable": True,
    }


def test_intelligence_reports_real_repo_signals(services, server, tmp_path) -> None:
    repo = _git_repo(tmp_path)
    (repo / "RINARI.md").write_text("# conventions\n")
    # Untrusted projects withhold their instructions (same rule as prompts).
    result = _ok(server.handle_line(_req("i1", "project.intelligence", {"path": str(repo)})))
    assert result["project"]["root"] == str(repo.resolve())
    repository = result["repository"]
    assert "python" in repository["languages"]
    assert repository["scanned_files"] >= 1
    # No fabrication: commands are null when the repo states none.
    assert repository["test_command"] is None
    assert result["index"]["indexed"] is False
    assert result["instructions"]["trusted"] is False
    assert result["instructions"]["scopes"] == []

    services.trust.add(repo)
    trusted = _ok(server.handle_line(_req("i2", "project.intelligence", {"path": str(repo)})))
    assert trusted["instructions"]["trusted"] is True
    scopes = trusted["instructions"]["scopes"]
    assert any(s["provenance"] == "./RINARI.md" and s["scope"] == "root" for s in scopes)


def test_project_trust_records_explicit_grant_and_enables_instructions(server, tmp_path) -> None:
    repo = _git_repo(tmp_path)
    (repo / "RINARI.md").write_text("# conventions\n")

    granted = _ok(server.handle_line(_req("trust", "project.trust", {"path": str(repo)})))
    assert granted["project"]["root"] == str(repo.resolve())
    assert granted["trust"]["state"] == "trusted"
    assert granted["trust"]["trusted_at"]

    intel = _ok(server.handle_line(_req("intel", "project.intelligence", {"path": str(repo)})))
    assert intel["instructions"]["trusted"] is True
    assert any(scope["provenance"] == "./RINARI.md" for scope in intel["instructions"]["scopes"])


def test_intelligence_rejects_non_directory(server, tmp_path) -> None:
    err = _err(
        server.handle_line(_req("i2", "project.intelligence", {"path": str(tmp_path / "nope")}))
    )
    assert err["code"] == "INVALID_PARAMS"


def test_project_crud_is_metadata_only_and_missing_folder_stays_visible(server, tmp_path) -> None:
    root = tmp_path / "registered"
    root.mkdir()
    added = _ok(
        server.handle_line(
            _req(
                "p1",
                "project.add",
                {"path": str(root), "name": "Registered", "description": "Desktop project"},
            )
        )
    )
    project = added["project"]
    assert added["created"] is True
    assert project["name"] == "Registered"
    assert not (root / ".rinari").exists()

    updated = _ok(
        server.handle_line(
            _req("p2", "project.update", {"project_id": project["id"], "pinned": True})
        )
    )["project"]
    assert updated["pinned"] is True
    listed = _ok(server.handle_line(_req("p3", "project.list", {})))["projects"]
    assert listed[0]["id"] == project["id"]

    root.rename(tmp_path / "moved-away")
    status = _ok(server.handle_line(_req("p4", "project.status", {"project_id": project["id"]})))
    assert status["exists"] is False
    assert status["git"]["error"]["code"] == "PROJECT_PATH_MISSING"


def test_session_rename_archive_restore_and_filters(server, tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    project = _ok(server.handle_line(_req("a1", "project.add", {"path": str(root)})))["project"]
    created = _ok(
        server.handle_line(
            _req("a2", "session.create", {"project_id": project["id"], "mode": "build"})
        )
    )["session"]
    assert created["kind"] == "PROJECT"
    renamed = _ok(
        server.handle_line(
            _req("a3", "session.rename", {"ref": created["id"], "title": "New title"})
        )
    )["session"]
    assert renamed["title"] == "New title"
    archived = _ok(server.handle_line(_req("a4", "session.archive", {"ref": created["id"]})))[
        "session"
    ]
    assert archived["state"] == "archived"
    filtered = _ok(
        server.handle_line(
            _req(
                "a5",
                "session.list",
                {"project_id": project["id"], "state": "archived", "include_closed": True},
            )
        )
    )["sessions"]
    assert [row["id"] for row in filtered] == [created["id"]]
    restored = _ok(server.handle_line(_req("a6", "session.restore", {"ref": created["id"]})))[
        "session"
    ]
    assert restored["state"] == "active"


def test_project_archive_policy_is_metadata_only_and_can_be_restored(server, tmp_path) -> None:
    root = tmp_path / "archive-me"
    root.mkdir()
    marker = root / "owned.txt"
    marker.write_text("user data", encoding="utf-8")
    project = _ok(server.handle_line(_req("pa1", "project.add", {"path": str(root)})))[
        "project"
    ]
    session = _ok(
        server.handle_line(_req("pa2", "session.create", {"project_id": project["id"]}))
    )["session"]

    removed = _ok(
        server.handle_line(
            _req(
                "pa3",
                "project.remove",
                {"project_id": project["id"], "session_policy": "archive"},
            )
        )
    )
    assert removed["project"]["archived"] is True
    assert removed["filesystem_deleted"] is False
    assert marker.read_text(encoding="utf-8") == "user data"
    assert _ok(server.handle_line(_req("pa4", "session.get", {"ref": session["id"]})))[
        "session"
    ]["state"] == "archived"

    rejected = _err(
        server.handle_line(
            _req("pa5", "session.create", {"project_id": project["id"]})
        )
    )
    assert rejected["code"] == "INVALID_PARAMS"
    restored_project = _ok(
        server.handle_line(
            _req(
                "pa6",
                "project.update",
                {"project_id": project["id"], "archived": False},
            )
        )
    )["project"]
    assert restored_project["archived"] is False


def test_chat_lifecycle_and_project_fork_identity(server, tmp_path) -> None:
    chat = _ok(server.handle_line(_req("lc1", "session.create", {"chat": True})))["session"]
    assert _ok(server.handle_line(_req("lc2", "session.archive", {"ref": chat["id"]})))[
        "session"
    ]["state"] == "archived"
    assert _ok(server.handle_line(_req("lc3", "session.restore", {"ref": chat["id"]})))[
        "session"
    ]["state"] == "active"

    root = tmp_path / "fork-project"
    root.mkdir()
    project = _ok(server.handle_line(_req("lc4", "project.add", {"path": str(root)})))[
        "project"
    ]
    source = _ok(
        server.handle_line(_req("lc5", "session.create", {"project_id": project["id"]}))
    )["session"]
    forked = _ok(server.handle_line(_req("lc6", "session.fork", {"ref": source["id"]})))[
        "session"
    ]
    assert forked["project_id"] == project["id"]
    assert forked["project_root"] == project["root"]
