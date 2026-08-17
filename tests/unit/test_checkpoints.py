import subprocess
from pathlib import Path

import pytest

from rinari.checkpoints import core
from rinari.checkpoints.service import CheckpointService
from rinari.shared.errors import InvalidUsageError


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    (root / "committed.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "committed.py")
    _git(root, "commit", "-qm", "init")
    return root


def _session(app_ctx, project_root: Path) -> str:
    from rinari.storage.records import SessionRecord

    now = "2026-01-01T00:00:00Z"
    rec = SessionRecord(
        id="ses_ckpt",
        kind="PROJECT",
        title="t",
        project_id=None,
        project_root_snapshot=str(project_root),
        created_cwd=str(project_root),
        current_cwd=str(project_root),
        provider_id="",
        model_id="",
        profile_id="",
        mode="default",
        state="active",
        compact_state=None,
        created_at=now,
        updated_at=now,
        last_active_at=now,
    )
    app_ctx.session_repo.insert(rec)
    return "ses_ckpt"


def test_create_and_restore_agent_change(app_ctx, repo):
    # session baseline captured while tree is clean
    from rinari.projects.worktree import snapshot_worktree
    from rinari.storage.records import WorktreeBaselineRecord

    sid = _session(app_ctx, repo)
    entries = snapshot_worktree(repo)  # empty (clean)
    base_now = "2026-01-01T00:00:01Z"
    recs = [
        WorktreeBaselineRecord(
            session_id=sid,
            path=p,
            git_status=status,
            blob_sha=sha,
            created_at=base_now,
        )
        for p, (status, sha) in entries.items()
    ]
    app_ctx.worktree_repo.insert_many(sid, recs)

    # agent modifies a committed file + creates a new file
    (repo / "committed.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "new.py").write_text("y = 2\n", encoding="utf-8")

    service = CheckpointService(app_ctx)
    cp = service.create(repo, label="before break", session_id=sid)
    assert cp["agent_changes"] == 2
    assert cp["user_owned"] == 0

    # then the agent breaks things further
    (repo / "committed.py").write_text("x = 999\n", encoding="utf-8")
    (repo / "new.py").write_text("boom\n", encoding="utf-8")

    preview = service.restore(repo, checkpoint_id=cp["id"], preview=True)
    assert all(op["action"] == "restore" for op in preview["operations"])

    result = service.restore(repo, checkpoint_id=cp["id"])
    assert sorted(result["applied"]) == ["committed.py", "new.py"]
    assert (repo / "committed.py").read_text() == "x = 2\n"
    assert (repo / "new.py").read_text() == "y = 2\n"
    assert result["skipped"] == []


def test_user_owned_not_touched(app_ctx, repo):
    from rinari.projects.worktree import snapshot_worktree
    from rinari.storage.records import WorktreeBaselineRecord

    # user had a dirty file before the session
    (repo / "user.txt").write_text("user work\n", encoding="utf-8")
    sid = _session(app_ctx, repo)
    entries = snapshot_worktree(repo)
    assert "user.txt" in entries
    base_now = "2026-01-01T00:00:01Z"
    app_ctx.worktree_repo.insert_many(
        sid,
        [
            WorktreeBaselineRecord(
                session_id=sid, path=p, git_status=s, blob_sha=sha, created_at=base_now
            )
            for p, (s, sha) in entries.items()
        ],
    )
    # agent adds a new file
    (repo / "agent.txt").write_text("agent work\n", encoding="utf-8")

    service = CheckpointService(app_ctx)
    cp = service.create(repo, session_id=sid)
    assert cp["user_owned"] >= 1

    # later, agent destroys user.txt and its own file
    (repo / "user.txt").write_text("DESTROYED\n", encoding="utf-8")
    (repo / "agent.txt").write_text("gone\n", encoding="utf-8")

    # Without allow-mixed, user.txt is skipped (user-owned: untracked at
    # baseline), agent.txt is restored.
    result = service.restore(repo, checkpoint_id=cp["id"])
    assert any(s["path"] == "user.txt" for s in result["skipped"])
    assert (repo / "user.txt").read_text() == "DESTROYED\n"  # untouched
    assert (repo / "agent.txt").read_text() == "agent work\n"


def test_mixed_allows_rewrite(app_ctx, repo):
    from rinari.projects.worktree import snapshot_worktree
    from rinari.storage.records import WorktreeBaselineRecord

    # Tracked file, user-dirty at baseline (content differs from HEAD).
    (repo / "mixed.txt").write_text("v0\n", encoding="utf-8")
    _git(repo, "add", "mixed.txt")
    _git(repo, "commit", "-qm", "add mixed")
    (repo / "mixed.txt").write_text("v1-user\n", encoding="utf-8")
    sid = _session(app_ctx, repo)
    entries = snapshot_worktree(repo)
    now = "2026-01-01T00:00:01Z"
    app_ctx.worktree_repo.insert_many(
        sid,
        [
            WorktreeBaselineRecord(
                session_id=sid, path=p, git_status=s, blob_sha=sha, created_at=now
            )
            for p, (s, sha) in entries.items()
        ],
    )
    (repo / "mixed.txt").write_text("v2-in-session\n", encoding="utf-8")
    service = CheckpointService(app_ctx)
    cp = service.create(repo, session_id=sid)

    (repo / "mixed.txt").write_text("v3\n", encoding="utf-8")
    result = service.restore(repo, checkpoint_id=cp["id"], allow_mixed=True)
    assert (repo / "mixed.txt").read_text() == "v2-in-session\n"
    assert result["skipped"] == []


def test_requires_git(app_ctx, tmp_path):
    plain = tmp_path / "nongit"
    plain.mkdir()
    service = CheckpointService(app_ctx)
    with pytest.raises(InvalidUsageError):
        service.create(plain, session_id="ses_x")


def test_list_and_remove(app_ctx, repo):
    sid = _session(app_ctx, repo)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    service = CheckpointService(app_ctx)
    cp1 = service.create(repo, session_id=sid)
    cp2 = service.create(repo, session_id=sid)
    items = service.list(repo)
    assert [c["id"] for c in items] == [cp2["id"], cp1["id"]]
    assert service.remove(cp1["id"]) is True
    assert service.remove(cp1["id"]) is False
    assert service.remove("missing") is False


def test_plan_restore_classifies(app_ctx, repo):
    entries = [
        core.CheckpointEntry("a.py", core.OWNERSHIP_AGENT, " M", None, b"a = 1\n", True),
        core.CheckpointEntry("u.py", core.OWNERSHIP_USER, "??", None, b"u\n", True),
        core.CheckpointEntry("m.py", core.OWNERSHIP_MIXED, " M", None, b"m\n", True),
        core.CheckpointEntry("d.py", core.OWNERSHIP_AGENT, "??", None, None, False),
    ]
    ops = core.plan_restore(entries, allow_mixed=False)
    by_path = {op.path: op.action for op in ops}
    assert by_path["a.py"] == "restore"
    assert by_path["u.py"] == "skip"
    assert by_path["m.py"] == "skip"
    assert by_path["d.py"] == "restore"
    assert (
        core.apply_restore(
            repo, core.CheckpointEntry("d.py", core.OWNERSHIP_AGENT, "??", None, None, False)
        )
        == "noop"
    )
