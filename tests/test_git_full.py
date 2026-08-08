"""Tests para el control de git completo: status rico, diff sin hacks, log, branch, stash, checkout, pull/push."""

import subprocess

import pytest

from rinari.agent.tools import (
    ToolRegistry,
    git_branch,
    git_checkout,
    git_commit,
    git_diff,
    git_log,
    git_pull,
    git_push,
    git_stash,
    git_status,
    is_dangerous,
)


@pytest.fixture
def git_repo(tmp_path):
    """Repo git con un commit inicial, un cambio staged y uno unstaged."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("linea1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    # staged
    (tmp_path / "b.txt").write_text("nuevo\n", encoding="utf-8")
    subprocess.run(["git", "add", "b.txt"], cwd=tmp_path, check=True)
    # unstaged + untracked
    (tmp_path / "a.txt").write_text("linea1\nlinea2\n", encoding="utf-8")
    (tmp_path / "u.txt").write_text("untracked\n", encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------- git_status rico

def test_git_status_separates_staged_unstaged(git_repo):
    result = git_status({}, cwd=str(git_repo))
    assert result["ok"] is True
    assert any("b.txt" in c["path"] for c in result["staged"])
    assert any("a.txt" in c["path"] for c in result["unstaged"])
    assert any("u.txt" in c["path"] for c in result["untracked"])
    # compat: changes = todos juntos
    assert len(result["changes"]) == len(result["staged"]) + len(result["unstaged"]) + len(result["untracked"])


def test_git_status_clean_has_no_sections(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    result = git_status({}, cwd=str(tmp_path))
    assert result["clean"] is True
    assert result["staged"] == []
    assert result["unstaged"] == []
    assert result["untracked"] == []
    assert result["counts"] == {"staged": 0, "unstaged": 0, "untracked": 0}


# ------------------------------------------------------------- git_diff limpio

def test_git_diff_no_index_mutation(git_repo):
    """git_diff NO debe tocar el index (antes usaba add -N + reset)."""
    result = git_diff({}, cwd=str(git_repo))
    assert result["ok"] is True
    assert "+linea2" in result["diff"]
    # el index no fue mutado: b.txt sigue staged
    status = subprocess.run(
        ["git", "-C", str(git_repo), "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout
    assert "A  b.txt" in status or "A b.txt" in status


def test_git_diff_has_sections(git_repo):
    result = git_diff({}, cwd=str(git_repo))
    assert result["ok"] is True
    assert "staged" in result
    assert "unstaged" in result
    assert "untracked" in result


def test_git_diff_path_filter(git_repo):
    result = git_diff({"path": "a.txt"}, cwd=str(git_repo))
    assert result["ok"] is True
    assert "+linea2" in result["diff"]
    assert "nuevo" not in result["diff"]  # b.txt/u.txt no aparecen


def test_git_diff_limit_lines(git_repo):
    result = git_diff({"max_lines": 5}, cwd=str(git_repo))
    assert result["ok"] is True
    assert result["truncated"] is True or len(result["diff"].splitlines()) <= 5


# ------------------------------------------------------------------ git_log

def test_git_log_lists_commits(git_repo):
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-am", "segundo"], check=True)
    result = git_log({}, cwd=str(git_repo))
    assert result["ok"] is True
    assert len(result["commits"]) >= 2
    first = result["commits"][0]
    assert "hash" in first and "message" in first and "author" in first
    assert "segundo" in first["message"] or any("segundo" in c["message"] for c in result["commits"])


def test_git_log_limit(git_repo):
    (git_repo / "a.txt").write_text("linea1\nlinea2\nlinea3\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-am", "s2"], check=True)
    (git_repo / "a.txt").write_text("linea1\nlinea2\nlinea3\nlinea4\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-am", "s3"], check=True)
    result = git_log({"limit": 2}, cwd=str(git_repo))
    assert len(result["commits"]) == 2


# ----------------------------------------------------------------- git_branch

def test_git_branch_lists_current(git_repo):
    result = git_branch({}, cwd=str(git_repo))
    assert result["ok"] is True
    assert result["current"] in ("master", "main")
    assert any(b["name"] == result["current"] and b["current"] for b in result["branches"])


def test_git_branch_creates_branch_listing(git_repo):
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature-x"], check=True)
    result = git_branch({}, cwd=str(git_repo))
    assert any(b["name"] == "feature-x" for b in result["branches"])


# ----------------------------------------------------------------- git_stash

def test_git_stash_push_pop(git_repo):
    push = git_stash({"action": "push"}, cwd=str(git_repo))
    assert push["ok"] is True
    lst = git_stash({"action": "list"}, cwd=str(git_repo))
    assert lst["ok"] is True
    assert lst["stashes"]  # al menos una
    pop = git_stash({"action": "pop"}, cwd=str(git_repo))
    assert pop["ok"] is True
    lst2 = git_stash({"action": "list"}, cwd=str(git_repo))
    assert lst2["stashes"] == []


def test_git_stash_invalid_action(git_repo):
    result = git_stash({"action": "wat"}, cwd=str(git_repo))
    assert result["ok"] is False


# --------------------------------------------------------------- git_checkout

def test_git_checkout_branch(git_repo):
    subprocess.run(["git", "-C", str(git_repo), "branch", "feature-y"], check=True)
    result = git_checkout({"branch": "feature-y"}, cwd=str(git_repo))
    assert result["ok"] is True
    branch = subprocess.run(
        ["git", "-C", str(git_repo), "branch", "--show-current"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert branch == "feature-y"


def test_git_checkout_missing_branch(git_repo):
    result = git_checkout({"branch": "no-existe"}, cwd=str(git_repo))
    assert result["ok"] is False


# ----------------------------------------------------------------- pull/push

def test_git_pull_missing_remote(git_repo):
    result = git_pull({}, cwd=str(git_repo))
    assert result["ok"] is False
    assert "remote" in result["error"].lower() or "upstream" in result["error"].lower() or "no" in result["error"].lower()


def test_git_push_missing_remote(git_repo):
    result = git_push({}, cwd=str(git_repo))
    assert result["ok"] is False


def test_git_push_pull_are_dangerous():
    assert is_dangerous("git push") is True
    assert is_dangerous("git pull") is True


def test_registry_exposes_all_git_tools(git_repo):
    registry = ToolRegistry()
    names = {s["function"]["name"] for s in registry.openai_schemas()}
    for name in ("git_status", "git_diff", "git_commit", "git_log", "git_branch",
                 "git_stash", "git_checkout", "git_pull", "git_push"):
        assert name in names, f"falta {name}"
