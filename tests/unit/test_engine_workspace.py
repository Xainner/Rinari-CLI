"""Engine Protocol slice 6a: tasks, verification, checkpoints, working tree."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.server import EngineServer

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git executable not available")


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


def test_task_tree_empty_and_get_not_found(server, tmp_path) -> None:
    result = _ok(server.handle_line(_req("w1", "task.tree", {"path": str(tmp_path)})))
    assert result["tasks"] == []
    assert result["depths"] == {}

    error = server.handle_line(
        _req("w2", "task.get", {"path": str(tmp_path), "task_id": "tsk-nope"})
    )
    assert error is not None and error["ok"] is False
    assert error["error"]["code"] == "NOT_FOUND"


def test_task_tree_lists_seeded_tasks(server, services, tmp_path) -> None:
    first = services.tasks.add(str(tmp_path), "First")
    services.tasks.add(str(tmp_path), "Second", depends_on=first["id"])
    result = _ok(server.handle_line(_req("w3", "task.tree", {"path": str(tmp_path)})))
    assert len(result["tasks"]) == 2
    assert result["depths"][first["id"]] == 0

    detail = _ok(
        server.handle_line(_req("w4", "task.get", {"path": str(tmp_path), "task_id": first["id"]}))
    )
    assert detail["task"]["title"] == "First"
    assert "done_when" in detail["task"]


def test_verification_latest_empty_and_plan_shape(server, tmp_path) -> None:
    result = _ok(server.handle_line(_req("w5", "verification.latest", {"path": str(tmp_path)})))
    assert result["records"] == []

    plan = _ok(
        server.handle_line(
            _req("w6", "verification.plan", {"path": str(tmp_path), "changed_files": []})
        )
    )["plan"]
    for key in (
        "changed",
        "tests",
        "targeted",
        "test_commands",
        "lint_commands",
        "typecheck_commands",
        "build_commands",
        "risk",
        "reasons",
    ):
        assert key in plan, key
    # JSON-safe: tuples serialized as lists.
    json.dumps(plan)


def test_checkpoint_list_empty_and_show_missing(server, tmp_path) -> None:
    # Global list (no path): empty home has no checkpoints anywhere.
    result = _ok(server.handle_line(_req("w7", "checkpoint.list", {})))
    assert result["checkpoints"] == []

    error = server.handle_line(_req("w8", "checkpoint.show", {"checkpoint_id": "chk-nope"}))
    assert error is not None and error["ok"] is False
    assert error["error"]["code"] == "INVALID_USAGE"


def test_project_changes_outside_repo(server, tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = _ok(server.handle_line(_req("w9", "project.changes", {"path": str(plain)})))
    assert result["available"] is False
    assert result["files"] == []


@needs_git
def test_project_changes_and_diff_in_repo(server, tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "a.txt").write_text(
        "one\ntwo\n" + "".join(f"line-{n}\n" for n in range(300)),
        encoding="utf-8",
    )
    (repo / "b.txt").write_text("new\n", encoding="utf-8")

    result = _ok(server.handle_line(_req("w10", "project.changes", {"path": str(repo)})))
    assert result["available"] is True
    assert result["dirty"] is True
    by_path = {f["path"]: f for f in result["files"]}
    assert set(by_path) == {"a.txt", "b.txt"}
    assert by_path["a.txt"]["unstaged"] == "M"
    assert by_path["b.txt"]["unstaged"] == "?"

    diff = _ok(
        server.handle_line(_req("w11", "project.diff", {"path": str(repo), "file": "a.txt"}))
    )
    assert diff["truncated"] is False
    assert "+two" in diff["diff"]

    small = _ok(
        server.handle_line(_req("w12", "project.diff", {"path": str(repo), "max_chars": 1000}))
    )
    assert small["truncated"] is True
    assert small["chars"] > 1000
    assert len(small["diff"]) == 1000
