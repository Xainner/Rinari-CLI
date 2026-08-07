"""Tests para las tools de git del agente (status/diff/commit)."""

import subprocess

import pytest

from rinari.agent.tools import (
    ToolRegistry,
    git_commit,
    git_diff,
    git_status,
    is_dangerous,
)


@pytest.fixture
def git_repo(tmp_path):
    """Repo git con un commit inicial y un cambio sin commitear."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("linea1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    # Cambio sin commitear
    (tmp_path / "a.txt").write_text("linea1\nlinea2\n", encoding="utf-8")
    (tmp_path / "nuevo.txt").write_text("nuevo\n", encoding="utf-8")
    return tmp_path


def test_git_status_shows_changes(git_repo):
    result = git_status({}, cwd=str(git_repo))
    assert result["ok"] is True
    assert result["branch"] in ("master", "main")
    assert len(result["changes"]) >= 2  # modificado + nuevo
    assert any("a.txt" in c["path"] for c in result["changes"])
    assert any("nuevo.txt" in c["path"] for c in result["changes"])


def test_git_status_clean_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    result = git_status({}, cwd=str(tmp_path))
    assert result["clean"] is True
    assert result["changes"] == []


def test_git_status_not_a_repo(tmp_path):
    result = git_status({}, cwd=str(tmp_path))
    assert result["ok"] is False
    assert "git" in result["error"].lower()


def test_git_diff_shows_changes(git_repo):
    result = git_diff({}, cwd=str(git_repo))
    assert result["ok"] is True
    assert "+linea2" in result["diff"]  # línea agregada
    assert "+nuevo" in result["diff"]  # archivo nuevo


def test_git_diff_limit_lines(git_repo):
    result = git_diff({"max_lines": 5}, cwd=str(git_repo))
    assert result["ok"] is True
    assert result["truncated"] is True or len(result["diff"].splitlines()) <= 5


def test_git_commit_creates_commit(git_repo):
    result = git_commit({"message": "feat: agrega linea2"}, cwd=str(git_repo))
    assert result["ok"] is True
    assert result["commit"] is not None
    # Verificar que el repo quedó limpio
    status = subprocess.run(
        ["git", "-C", str(git_repo), "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert status == ""


def test_git_commit_requires_message(git_repo):
    result = git_commit({}, cwd=str(git_repo))
    assert result["ok"] is False
    assert "mensaje" in result["error"].lower()


def test_git_commit_not_a_repo(tmp_path):
    result = git_commit({"message": "x"}, cwd=str(tmp_path))
    assert result["ok"] is False


def test_git_commit_is_dangerous():
    """git commit requiere aprobación (modifica el historial del repo)."""
    assert is_dangerous("git commit -m 'x'") is True
    assert is_dangerous("git status") is False
    assert is_dangerous("git diff") is False


def test_registry_exposes_git_tools():
    registry = ToolRegistry()
    names = {s["function"]["name"] for s in registry.openai_schemas()}
    assert "git_status" in names
    assert "git_diff" in names
    assert "git_commit" in names


def test_registry_executes_git_status(git_repo):
    registry = ToolRegistry()
    result = registry.execute("git_status", {}, cwd=str(git_repo))
    assert result["ok"] is True
