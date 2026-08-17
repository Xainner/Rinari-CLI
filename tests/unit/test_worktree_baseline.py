"""Dirty-worktree baseline + user-change preservation (phase 3 items:
dirty-worktree baseline, preserve user changes, safe diff ownership metadata).

Regression contract: uncommitted changes that predate the session are
user-owned. The runtime (not the prompt) forces approval before overwriting
them and tags ownership in git.status.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli.agent_runtime import _ensure_worktree_baseline
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import (
    CAPABILITY_FS_WRITE,
    PolicyAction,
    PolicyEngine,
    SessionScope,
    normalize_profile,
)
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.projects.worktree import WorktreeGuard, snapshot_worktree
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.tools.definition import ToolContext, ToolErrorCode
from rinari.tools.native import all_native_tools
from rinari.tools.native.fs import fs_write
from rinari.tools.native.git import git_status
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@test")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "core.autocrlf", "false")
    (root / "a.txt").write_text("committed\n", encoding="utf-8")
    (root / "b.txt").write_text("stays clean\n", encoding="utf-8")
    _git(root, "add", "a.txt", "b.txt")
    _git(root, "commit", "-q", "-m", "initial")
    # Pre-existing uncommitted user work:
    (root / "a.txt").write_text("committed\nuser edit\n", encoding="utf-8")
    (root / "untracked.txt").write_text("user file\n", encoding="utf-8")
    return root


def test_snapshot_captures_preexisting_dirt(repo) -> None:
    entries = snapshot_worktree(repo)
    assert set(entries) == {"a.txt", "untracked.txt"}
    assert entries["a.txt"][0].startswith("M")
    assert entries["untracked.txt"][0].startswith("?")


def test_guard_classification(repo) -> None:
    baseline = snapshot_worktree(repo)
    guard = WorktreeGuard(repo, baseline)
    assert guard.classify(repo / "a.txt") == "user-dirty"
    assert guard.classify(repo / "b.txt") is None  # committed, unchanged

    (repo / "a.txt").write_text("committed\nuser edit\nagent edit\n", encoding="utf-8")
    assert guard.classify(repo / "a.txt") == "modified-in-session"

    user_dir = repo / "newdir"
    user_dir.mkdir()
    (user_dir / "mine.txt").write_text("x\n", encoding="utf-8")
    baseline2 = snapshot_worktree(repo)
    assert "newdir" in baseline2
    guard2 = WorktreeGuard(repo, baseline2)
    assert guard2.classify(user_dir / "mine.txt") == "user-dirty"


def test_policy_asks_to_overwrite_user_dirty(repo) -> None:
    baseline = snapshot_worktree(repo)
    guard = WorktreeGuard(repo, baseline)
    engine = PolicyEngine()
    scope = SessionScope(
        kind="PROJECT",
        root=repo,
        cwd=repo,
        profile=normalize_profile("workspace"),
        user_home=repo.parent,
        worktree=guard,
    )
    decision = engine.decide(CAPABILITY_FS_WRITE, scope, path="a.txt")
    assert decision.action is PolicyAction.ASK
    assert "uncommitted user changes" in decision.reason

    plain = engine.decide(CAPABILITY_FS_WRITE, scope, path="b.txt")
    assert plain.action is PolicyAction.ALLOW

    scope_no_guard = SessionScope(
        kind="PROJECT",
        root=repo,
        cwd=repo,
        profile=normalize_profile("workspace"),
        user_home=repo.parent,
    )
    assert (
        engine.decide(CAPABILITY_FS_WRITE, scope_no_guard, path="a.txt").action
        is PolicyAction.ALLOW
    )


def _tool_ctx(repo: Path, guard) -> ToolContext:
    return ToolContext(
        session_id="s1",
        kind="PROJECT",
        cwd=repo,
        project_root=repo,
        user_home=repo.parent,
        profile=normalize_profile("workspace"),
        sandbox=FilesystemSandbox(read_root=repo, write_roots=(repo,)),
        limits=ProcessLimits(timeout_s=10, max_output_bytes=1024 * 1024),
        artifact_root=repo / "artifacts",
        clock=FakeClock(start=1_700_000_000.0, step=0.1),
        cancellation=CancellationToken(),
        worktree=guard,
    )


def _runtime(ctx: ToolContext, answer: str) -> ToolRuntime:
    registry = ToolRegistry()
    registry.register_all(all_native_tools())
    return ToolRuntime(
        registry,
        PolicyEngine(),
        ApprovalEngine(prompt=lambda req: answer),
        clock=FakeClock(start=1_700_000_000.0, step=0.1),
    )


def test_unapproved_overwrite_is_denied_and_user_work_preserved(repo) -> None:
    guard = WorktreeGuard(repo, snapshot_worktree(repo))
    runtime = _runtime(_tool_ctx(repo, guard), answer="n")
    before = (repo / "a.txt").read_text(encoding="utf-8")
    result = runtime.execute(
        "fs.write", {"path": "a.txt", "content": "agent overwrite\n"}, _tool_ctx(repo, guard)
    )
    assert not result.ok
    assert result.error.code is ToolErrorCode.APPROVAL_DENIED
    # The user's uncommitted edit survived:
    assert (repo / "a.txt").read_text(encoding="utf-8") == before


def test_approved_overwrite_flags_user_pre_existing_changes(repo) -> None:
    guard = WorktreeGuard(repo, snapshot_worktree(repo))
    result = fs_write({"path": "a.txt", "content": "new content\n"}, _tool_ctx(repo, guard))
    assert result.ok
    assert result.data["user_pre_existing_changes"] is True
    assert "uncommitted user changes" in result.data["warning"]

    # A brand-new file is the agent's own work: no flag.
    result_new = fs_write({"path": "agent-file.txt", "content": "new\n"}, _tool_ctx(repo, guard))
    assert result_new.ok
    assert "user_pre_existing_changes" not in result_new.data


def test_git_status_ownership_metadata(repo) -> None:
    guard = WorktreeGuard(repo, snapshot_worktree(repo))
    ctx = _tool_ctx(repo, guard)
    (repo / "a.txt").write_text("committed\nuser edit\nagent edit\n", encoding="utf-8")
    (repo / "agent-file.txt").write_text("new\n", encoding="utf-8")
    result = git_status({}, ctx)
    assert result.ok
    by_path = {f["path"]: f for f in result.data["files"]}
    assert by_path["a.txt"]["ownership"] == "modified-in-session"
    assert by_path["untracked.txt"]["ownership"] == "user"
    assert by_path["agent-file.txt"]["ownership"] == "new-in-session"
    assert "ownership_legend" in result.data


def test_baseline_captured_once_and_preserved(app_ctx, tmp_path) -> None:
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

    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@test")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "initial")
    (repo / "a.txt").write_text("v1\nuser edit\n", encoding="utf-8")

    record = s.sessions.start(repo).session
    assert record.kind == "PROJECT"
    guard = _ensure_worktree_baseline(s, record)
    assert guard is not None
    assert guard.classify(repo / "a.txt") == "user-dirty"
    assert s.ctx.worktree_repo.exists(record.id)

    # Simulate the agent writing in the meantime (explicitly approved work):
    (repo / "a.txt").write_text("v1\nuser edit\nagent edit\n", encoding="utf-8")
    (repo / "agent-file.txt").write_text("new\n", encoding="utf-8")

    # A second invocation must reuse the fixed baseline, not re-snapshot:
    guard2 = _ensure_worktree_baseline(s, record)
    assert guard2 is not None
    assert guard2.classify(repo / "a.txt") == "modified-in-session"
    assert guard2.classify(repo / "agent-file.txt") is None
