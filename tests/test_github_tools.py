"""Tests de las tools de GitHub del agente (PRs vía API)."""

import sys

import pytest

from rinari.agent.tools import (
    ToolRegistry,
    _gh_headers,
    _gh_remote_info,
    github_create_pr,
    github_list_prs,
)


@pytest.fixture
def git_repo(tmp_path):
    """Repo git con remote origin y rama actual."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/Xainner/Rinari-CLI.git"],
        cwd=tmp_path, check=True,
    )
    return tmp_path


def test_gh_remote_info_parses_url(git_repo):
    owner, repo = _gh_remote_info(str(git_repo))
    assert owner == "Xainner"
    assert repo == "Rinari-CLI"


def test_gh_remote_info_no_remote(tmp_path):
    result = _gh_remote_info(str(tmp_path))
    assert result is None


def test_gh_headers_requires_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    headers = _gh_headers()
    assert "Authorization" not in headers


def test_gh_headers_with_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    headers = _gh_headers()
    assert headers["Authorization"] == "Bearer secret-token"
    assert headers["Accept"] == "application/vnd.github+json"


def test_shell_for_platform_windows_prefers_git_bash(monkeypatch):
    """En Windows, usa el bash de Git por ruta (nunca el WSL del PATH)."""
    import rinari.agent.tools as tools_mod

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("SHELL", raising=False)
    shell = tools_mod._shell_for_platform()
    assert "Git" in shell  # C:\Program Files\Git\bin\bash.exe


def test_shell_for_platform_unix_uses_sh(monkeypatch):
    import rinari.agent.tools as tools_mod

    monkeypatch.setattr(sys, "platform", "linux")
    assert tools_mod._shell_for_platform() == "sh"


def test_run_command_uses_git_bash_not_path_bash(monkeypatch):
    """run_command ejecuta con el shell de Git, no con 'bash' del PATH."""
    import rinari.agent.tools as tools_mod

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("SHELL", raising=False)
    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            self.returncode = 0
            self.stdout = None
            self.stderr = None

        def communicate(self, timeout=None):
            return ("ok", "")

    monkeypatch.setattr(tools_mod.subprocess, "Popen", FakePopen)
    result = tools_mod.run_command({"command": "echo hi"}, cwd=".")
    assert result["exit_code"] == 0
    shell = captured["cmd"][0]
    assert "Git" in shell  # nunca System32\bash.exe (WSL)
    assert "bash.exe" not in shell.lower().replace("git", "") or "git" in shell.lower()


def test_create_pr_requires_token(git_repo, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = github_create_pr({"title": "Hola"}, cwd=str(git_repo))
    assert result["ok"] is False
    assert "token" in result["error"].lower() or "GITHUB" in result["error"]


def test_create_pr_no_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    result = github_create_pr({"title": "Hola"}, cwd=str(tmp_path))
    assert result["ok"] is False


def test_create_pr_posts_to_api(git_repo, monkeypatch):
    """Crea el PR con POST /repos/{owner}/{repo}/pulls."""
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    posted = {}

    class FakeResponse:
        status_code = 201
        text = '{"number": 42, "html_url": "https://github.com/x/y/pull/42"}'

        def json(self):
            return {"number": 42, "html_url": "https://github.com/x/y/pull/42"}

    def fake_post(url, headers=None, json=None, timeout=None):
        posted["url"] = url
        posted["headers"] = headers
        posted["json"] = json
        return FakeResponse()

    import rinari.agent.tools as tools_mod

    monkeypatch.setattr(tools_mod.httpx, "post", fake_post)
    result = github_create_pr({"title": "feat: algo", "body": "descripción"}, cwd=str(git_repo))
    assert result["ok"] is True
    assert result["number"] == 42
    assert "/repos/Xainner/Rinari-CLI/pulls" in posted["url"]
    assert posted["json"]["title"] == "feat: algo"
    assert "head" in posted["json"]  # la rama actual
    assert posted["json"]["base"] == "main"


def test_create_pr_api_error(git_repo, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")

    class FakeResponse:
        status_code = 422
        text = '{"message": "No commits between branches"}'

    def fake_post(url, headers=None, json=None, timeout=None):
        return FakeResponse()

    import rinari.agent.tools as tools_mod

    monkeypatch.setattr(tools_mod.httpx, "post", fake_post)
    result = github_create_pr({"title": "x"}, cwd=str(git_repo))
    assert result["ok"] is False
    assert "422" in result["error"]


def test_list_prs_gets_repo(git_repo, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    got = {}

    class FakeResponse:
        status_code = 200
        text = "[]"

        def json(self):
            return [
                {"number": 1, "title": "PR uno", "state": "open", "html_url": "u1"},
                {"number": 2, "title": "PR dos", "state": "open", "html_url": "u2"},
            ]

    def fake_get(url, headers=None, params=None, timeout=None):
        got["url"] = url
        got["params"] = params
        return FakeResponse()

    import rinari.agent.tools as tools_mod

    monkeypatch.setattr(tools_mod.httpx, "get", fake_get)
    result = github_list_prs({}, cwd=str(git_repo))
    assert result["ok"] is True
    assert len(result["pulls"]) == 2
    assert result["pulls"][0]["title"] == "PR uno"
    assert got["params"]["state"] == "open"
    assert "/repos/Xainner/Rinari-CLI/pulls" in got["url"]


def test_registry_exposes_github_tools(git_repo):
    registry = ToolRegistry()
    names = {s["function"]["name"] for s in registry.openai_schemas()}
    assert "github_create_pr" in names
    assert "github_list_prs" in names
