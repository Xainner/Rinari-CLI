"""The conversation's repository for «Resumen» (plan 0.4.0 #82)."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from rinari.engine_protocol import repo
from rinari.projects._git_process import GitCommandResult
from tests.unit import test_checklist as base
from tests.unit.test_checklist import _call

services = base.services
server = base.server


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "proyecto"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "a.txt").write_text("uno\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "inicio")
    return root


@pytest.fixture(autouse=True)
def _no_cache():
    repo._remote_cache.clear()
    yield
    repo._remote_cache.clear()


def test_the_remote_never_carries_a_credential():
    assert repo.safe_remote("https://x-access-token:ghp_secret@github.com/acme/app.git") == (
        "https://github.com/acme/app.git"
    )
    assert repo.safe_remote("ssh://git:pw@host:2222/r.git") == "ssh://host:2222/r.git"
    assert repo.safe_remote("git@github.com:acme/app.git") == "git@github.com:acme/app.git"
    assert repo.github_of("git@github.com:acme/app.git") == {"owner": "acme", "repo": "app"}
    assert repo.github_of("https://github.com/acme/app") == {"owner": "acme", "repo": "app"}
    assert repo.github_of("https://gitlab.com/acme/app.git") is None


def test_checks_roll_up_to_one_state():
    assert repo.checks_of([]) is None
    assert repo.checks_of([{"conclusion": "SUCCESS"}, {"state": "SUCCESS"}]) == "success"
    assert repo.checks_of([{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}]) == "pending"
    assert repo.checks_of([{"conclusion": "FAILURE"}, {"status": "QUEUED"}]) == "failure"


def test_local_status_reads_branch_changes_and_github(project, tmp_path):
    assert repo.local_status(tmp_path / "nada")["available"] is False
    plain = tmp_path / "suelta"
    plain.mkdir()
    assert repo.local_status(plain)["available"] is False

    _git(project, "remote", "add", "origin", "https://tok:secret@github.com/acme/app.git")
    (project / "a.txt").write_text("dos\n", encoding="utf-8")
    (project / "sub").mkdir()
    status = repo.local_status(project / "sub")  # from a subfolder too
    assert status["available"] and Path(status["root"]).resolve() == project.resolve()
    assert status["branch"] == "main" and status["upstream"] is None
    assert status["dirty"] is True and status["changed"] == 1
    assert status["remote_url"] == "https://github.com/acme/app.git"
    assert status["github"] == {"owner": "acme", "repo": "app"}


def _session(server, cwd: Path) -> str:
    return _call(server, "session.create", {"cwd": str(cwd), "chat": True})["result"]["session"][
        "id"
    ]


def _wait_event(server, name, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for evt in server.drain_events():
            if evt.get("event") == name:
                return evt["payload"]
        time.sleep(0.02)
    raise AssertionError(f"{name} did not arrive")


def test_github_part_arrives_by_event_then_comes_from_the_cache(server, project, monkeypatch):
    _git(project, "remote", "add", "origin", "https://github.com/acme/app.git")
    pr = {"number": 7, "title": "Tarjetas", "state": "open", "url": "u", "checks": "pending"}
    calls = []

    def remote_info(key):
        calls.append(key)
        info = {"gh": "ready", "prs": [pr]}
        repo._remote_cache[key] = (time.monotonic(), info)
        return info

    monkeypatch.setattr(repo, "remote_info", remote_info)
    session_id = _session(server, project)
    first = _call(server, "repo.status", {"session_id": session_id})["result"]
    assert first["available"] and first["remote_pending"] is True and first["prs"] == []
    updated = _wait_event(server, "repo.status.updated")
    assert updated["session_id"] == session_id and updated["prs"] == [pr]
    assert updated["gh"] == "ready" and updated["remote_pending"] is False
    second = _call(server, "repo.status", {"session_id": session_id})["result"]
    assert second["remote_pending"] is False and second["prs"] == [pr]
    assert len(calls) == 1


def test_a_repo_without_github_asks_nothing(server, project, monkeypatch):
    monkeypatch.setattr(repo, "remote_info", lambda key: pytest.fail("no GitHub to ask"))
    session_id = _session(server, project)
    status = _call(server, "repo.status", {"session_id": session_id})["result"]
    assert status["available"] and status["github"] is None
    assert status["remote_pending"] is False and status["gh"] is None


def test_pr_create_refuses_an_unpushed_branch_at_once(server, project):
    _git(project, "remote", "add", "origin", "https://github.com/acme/app.git")
    _git(project, "checkout", "-q", "-b", "tarjetas")
    session_id = _session(server, project)
    response = _call(
        server,
        "repo.pr.create",
        {"session_id": session_id, "title": "Tarjetas", "base": "main", "body": ""},
    )
    assert not response["ok"] and response["error"]["code"] == "NOT_PUSHED"


def _pushed_branch(project: Path, tmp_path: Path) -> None:
    bare = tmp_path / "remoto.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(project, "remote", "add", "origin", str(bare))
    _git(project, "checkout", "-q", "-b", "tarjetas")
    _git(project, "push", "-q", "-u", "origin", "tarjetas")
    _git(project, "remote", "set-url", "origin", "https://github.com/acme/app.git")


def test_pr_create_runs_gh_in_the_background_and_reports(server, project, tmp_path, monkeypatch):
    _pushed_branch(project, tmp_path)
    seen = []

    def fake_create(status, title, base, body):
        seen.append((status["branch"], title, base, body))
        return {"url": "https://github.com/acme/app/pull/12", "number": 12}

    monkeypatch.setattr(repo, "pr_create", fake_create)
    monkeypatch.setattr(repo, "remote_info", lambda key: {"gh": "ready", "prs": []})
    session_id = _session(server, project)
    started = _call(
        server,
        "repo.pr.create",
        {"session_id": session_id, "title": "Tarjetas", "base": "main", "body": "Hola"},
    )
    assert started["ok"] and started["result"]["status"] == "started"
    created = _wait_event(server, "repo.pr.created")
    assert created["number"] == 12 and created["job_id"] == started["result"]["job_id"]
    assert seen == [("tarjetas", "Tarjetas", "main", "Hola")]


def test_gh_output_gives_the_url_and_number(project, tmp_path, monkeypatch):
    _pushed_branch(project, tmp_path)
    status = repo.local_status(project)
    argv_seen = []

    def fake_run(argv, cwd, **kwargs):
        argv_seen.append(argv[1:3])
        if argv[1:3] == ["auth", "status"]:
            return GitCommandResult(stdout="", returncode=0)
        return GitCommandResult(
            stdout="Creating...\nhttps://github.com/acme/app/pull/34\n", returncode=0
        )

    monkeypatch.setattr(repo.shutil, "which", lambda name: "gh")
    monkeypatch.setattr(repo, "run_process", fake_run)
    assert repo.pr_create(status, "T", "main", "") == {
        "url": "https://github.com/acme/app/pull/34",
        "number": 34,
    }
    assert argv_seen == [["auth", "status"], ["pr", "create"]]

    monkeypatch.setattr(repo, "run_process", lambda argv, cwd, **k: GitCommandResult("", 1))
    with pytest.raises(repo.RepoError) as excinfo:
        repo.pr_create(status, "T", "main", "")
    assert excinfo.value.code == "GH_NOT_READY"
