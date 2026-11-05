"""Branch per project from .git/HEAD, without running git."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from rinari.projects import git_head
from rinari.projects.git_head import GitHead, HeadCache, find_git_dir, read_head

SHA = "0123456789abcdef0123456789abcdef01234567"


def _repo(root: Path, head: str = "ref: refs/heads/main\n") -> Path:
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text(head, encoding="utf-8")
    return git_dir


def test_branch_from_a_normal_repository(tmp_path: Path) -> None:
    _repo(tmp_path, "ref: refs/heads/feat/new-ui\n")
    assert read_head(tmp_path) == GitHead(branch="feat/new-ui")


def test_not_a_repository_and_missing_folder_are_empty(tmp_path: Path) -> None:
    assert read_head(tmp_path / "plain") == GitHead()
    (tmp_path / "plain").mkdir()
    assert read_head(tmp_path / "plain").branch is None


def test_detached_head_and_rebase_in_progress(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path, SHA + "\n")
    assert read_head(tmp_path) == GitHead(detached=True, sha_short=SHA[:7])
    (git_dir / "rebase-merge").mkdir()
    (git_dir / "rebase-merge" / "head-name").write_text("refs/heads/topic\n", encoding="utf-8")
    assert read_head(tmp_path) == GitHead(
        branch="topic", detached=True, sha_short=SHA[:7], operation="rebase"
    )


def test_garbage_head_is_empty(tmp_path: Path) -> None:
    _repo(tmp_path, "not a ref at all\n")
    assert read_head(tmp_path) == GitHead()


def test_project_nested_inside_a_repository(tmp_path: Path) -> None:
    _repo(tmp_path, "ref: refs/heads/develop\n")
    nested = tmp_path / "packages" / "web"
    nested.mkdir(parents=True)
    assert read_head(nested).branch == "develop"


@pytest.mark.parametrize("relative", [True, False])
def test_worktree_gitdir_file(tmp_path: Path, relative: bool) -> None:
    main = tmp_path / "main"
    main_git = _repo(main)
    wt_git = main_git / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/side\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    target = os.path.relpath(wt_git, worktree) if relative else str(wt_git)
    (worktree / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    assert find_git_dir(worktree) == wt_git.resolve()
    assert read_head(worktree).branch == "side"


def test_submodule_gitdir_file(tmp_path: Path) -> None:
    parent_git = _repo(tmp_path)
    module_git = parent_git / "modules" / "lib"
    module_git.mkdir(parents=True)
    (module_git / "HEAD").write_text(SHA + "\n", encoding="utf-8")
    module = tmp_path / "lib"
    module.mkdir()
    (module / ".git").write_text("gitdir: ../.git/modules/lib\n", encoding="utf-8")
    assert read_head(module) == GitHead(detached=True, sha_short=SHA[:7])


def test_cache_sees_a_branch_switch_and_a_new_repository(tmp_path: Path) -> None:
    cache = HeadCache()
    plain = tmp_path / "later"
    plain.mkdir()
    assert cache.get(plain).branch is None
    git_dir = _repo(plain, "ref: refs/heads/main\n")
    assert cache.get(plain).branch == "main"
    head = git_dir / "HEAD"
    head.write_text("ref: refs/heads/release-2\n", encoding="utf-8")
    stat = head.stat()
    os.utime(head, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000))
    assert cache.get(plain).branch == "release-2"


def test_reading_heads_never_starts_a_process(tmp_path: Path, monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("git_head must not run subprocesses")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    _repo(tmp_path, "ref: refs/heads/main\n")
    assert git_head.HeadCache().get(tmp_path).branch == "main"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_a_branch_has_its_commit_from_loose_or_packed_refs(tmp_path: Path) -> None:
    """sha_short was filled only for a detached HEAD (H-17)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    first = _git(repo, "rev-parse", "HEAD")
    assert read_head(repo) == GitHead(branch="main", sha_short=first[:7])
    cache = HeadCache()
    assert cache.get(repo).sha_short == first[:7]
    # A commit moves the branch without touching HEAD: the cache sees it.
    (repo / "a.txt").write_text("b", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "second")
    second = _git(repo, "rev-parse", "HEAD")
    assert cache.get(repo).sha_short == second[:7]
    # Packed refs only (after gc or a clone).
    _git(repo, "pack-refs", "--all")
    assert not (repo / ".git" / "refs" / "heads" / "main").exists()
    assert read_head(repo).sha_short == second[:7]
    # A worktree's branch lives in the main repository.
    _git(repo, "worktree", "add", "-q", "-b", "topic", str(tmp_path / "wt"))
    assert read_head(tmp_path / "wt") == GitHead(branch="topic", sha_short=second[:7])
