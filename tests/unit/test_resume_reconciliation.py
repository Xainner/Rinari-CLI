"""Resume reconciliation (phase 4).

Regression contract: resuming a session re-verifies the durable facts it
assumes — project identity, git branch, working tree, permission profile,
provider/model existence, trust, recorded cwd — and surfaces every miss as a
warning. The only automatic fix is re-registering a missing project identity
row. A branch switch or external worktree change must be detected, not
silently absorbed.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.reconcile import OK, ResumeReconciler
from rinari.application.services import build_services
from rinari.cli.agent_runtime import build_agent_session
from rinari.projects.git import git_state
from rinari.shared.clock import now_iso
from rinari.storage.records import SessionRecord, WorktreeBaselineRecord

SUBSYSTEMS = {
    "identity",
    "git-branch",
    "working-tree",
    "permissions",
    "provider",
    "model",
    "trust",
    "assumptions",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finding(started, subsystem: str):
    return next(f for f in started.findings if f.subsystem == subsystem)


@pytest.fixture
def env(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    s = build_services(app_ctx, user_home=user_home)
    s.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    s.models.add("fake", "fake-model-1", "fake-one")
    s.providers.use("fake")
    return app_ctx, user_home, s


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "checkout", "-qb", "main")
    _git(root, "config", "user.email", "test@test")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "core.autocrlf", "false")
    (root / "a.txt").write_text("committed\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _subsystems(findings) -> set[str]:
    return {f.subsystem for f in findings}


def test_chat_resume_reconciles_clean(env, tmp_path) -> None:
    s = env[2]
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    record = s.sessions.start(cwd).session
    assert record.kind == "CHAT"
    started = s.sessions.resume(record.id)
    assert _subsystems(started.findings) >= SUBSYSTEMS
    assert all(f.state == OK for f in started.findings)
    assert started.warnings == ()


def test_project_resume_clean_with_stored_branch(env, repo) -> None:
    s = env[2]
    record = s.sessions.start(repo).session
    assert record.kind == "PROJECT"
    s.trust.add(repo)
    branch = git_state(repo).branch
    record.git_branch = branch
    s.ctx.session_repo.update(record)
    started = s.sessions.resume(record.id)
    assert all(f.state == OK for f in started.findings), started.warnings


def test_resume_detects_branch_switch(env, repo) -> None:
    s = env[2]
    record = s.sessions.start(repo).session
    s.trust.add(repo)
    record.git_branch = "main"
    s.ctx.session_repo.update(record)
    _git(repo, "checkout", "-qb", "feature")
    started = s.sessions.resume(record.id)
    finding = _finding(started, "git-branch")
    assert finding.state == "changed"
    assert "main -> feature" in finding.detail
    assert any("git-branch" in w for w in started.warnings)


def test_resume_detects_external_worktree_change(env, repo) -> None:
    s = env[2]
    (repo / "untracked.txt").write_text("user file\n", encoding="utf-8")
    record = s.sessions.start(repo).session
    s.trust.add(repo)
    ts = now_iso(s.ctx.clock)
    s.ctx.worktree_repo.insert_many(
        record.id,
        [
            WorktreeBaselineRecord(
                session_id=record.id,
                path="untracked.txt",
                git_status="??",
                blob_sha=_sha(repo / "untracked.txt"),
                created_at=ts,
            )
        ],
    )
    started = s.sessions.resume(record.id)
    assert all(f.state == OK for f in started.findings), started.warnings

    (repo / "external.txt").write_text("external change\n", encoding="utf-8")
    started = s.sessions.resume(record.id)
    finding = _finding(started, "working-tree")
    assert finding.state == "changed"
    assert "external.txt" in finding.detail
    assert "untracked.txt" not in finding.detail


def test_resume_missing_project_root_warns(env, repo, tmp_path) -> None:
    s = env[2]
    record = s.sessions.start(repo).session
    # Move instead of rmtree: git object files are read-only on Windows.
    shutil.move(str(repo), str(tmp_path / "proj-moved"))
    started = s.sessions.resume(record.id)
    assert _finding(started, "identity").state == "missing"
    assert any("identity" in w for w in started.warnings)
    # no git comparisons run against a vanished root
    assert _finding(started, "git-branch").state == OK
    assert _finding(started, "working-tree").state == OK


def test_resume_re_registers_missing_project_row(env, repo, tmp_path) -> None:
    # FKs block deleting the project a session references, so simulate the
    # gap: the session's stored root points at a second repo that was never
    # registered as a project.
    from dataclasses import replace

    s = env[2]
    record = s.sessions.start(repo).session
    old_project_id = record.project_id
    assert old_project_id is not None

    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "user.email", "test@test")
    _git(other, "config", "user.name", "Test")
    (other / "x.txt").write_text("x\n", encoding="utf-8")
    _git(other, "add", "x.txt")
    _git(other, "commit", "-q", "-m", "initial")
    s.trust.add(other)

    repointed = replace(record, project_root_snapshot=str(other.resolve()))
    s.ctx.session_repo.update(repointed)
    started = s.sessions.resume(record.id)
    assert _finding(started, "identity").state == "fixed"
    assert started.session.project_id is not None
    assert started.session.project_id != old_project_id
    assert s.ctx.project_repo.get_by_root(str(other.resolve())) is not None


def test_reconcile_flags_missing_provider_and_model(env, tmp_path) -> None:
    # FKs prevent deleting a provider/model a live session references, so the
    # reconciler is exercised directly with a record pointing at vanished rows.
    ctx = env[0]
    s = env[2]
    record = SessionRecord(
        id="ses_ghost",
        kind="CHAT",
        title="ghost",
        project_id=None,
        project_root_snapshot=None,
        created_cwd=str(tmp_path),
        current_cwd=str(tmp_path),
        provider_id="prov_gone",
        model_id="model_gone",
        profile_id="workspace",
        mode="ask",
        state="active",
        compact_state=None,
        created_at="t",
        updated_at="t",
        last_active_at="t",
    )
    _, report = ResumeReconciler(ctx, s.projects, s.trust).reconcile(record)
    states = {f.subsystem: f.state for f in report.findings}
    assert states["provider"] == "missing"
    assert states["model"] == "missing"
    assert "provider" in report.warnings[0]


def test_resume_detects_permission_profile_change(env, tmp_path) -> None:
    from dataclasses import replace

    s = env[2]
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    record = s.sessions.start(cwd).session
    assert record.profile_id == "workspace"
    updated = replace(record, profile_id="read-only")
    s.ctx.session_repo.update(updated)
    started = s.sessions.resume(record.id)
    finding = _finding(started, "permissions")
    assert finding.state == "changed"
    assert "workspace" in finding.detail
    assert "read-only" in finding.detail


def test_branch_captured_on_first_agent_build(env, repo) -> None:
    s = env[2]
    record = s.sessions.start(repo).session
    branch = git_state(repo).branch
    build_agent_session(s, record, interactive=False)
    assert record.git_branch == branch
    assert s.ctx.session_repo.get(record.id).git_branch == branch


def test_start_resumes_existing_session_and_reconciles(env, tmp_path) -> None:
    s = env[2]
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    first = s.sessions.start(cwd)
    assert first.created is True
    second = s.sessions.start(cwd)
    assert second.created is False
    assert second.session.id == first.session.id
    assert second.findings
    assert second.warnings == ()
