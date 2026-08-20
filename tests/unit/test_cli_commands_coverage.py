"""Phase 7 command coverage: the public CLI groups from commands.md.

Covers the deterministic (no model call) subcommands end-to-end through the
Typer app with an isolated RINARI_HOME and working directory.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from rinari.cli.main import app
from rinari.shared.paths import ENV_HOME

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "rinari-home"))
    monkeypatch.chdir(work)
    return tmp_path, work


def _call(*args):
    return runner.invoke(app, list(args), catch_exceptions=False)


def _git_init(work):
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    (work / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=work,
        check=True,
    )
    return work


def _provider():
    res = _call("providers", "add", "openai", "--name", "fake", "--api-key", "sk-fake-test")
    assert res.exit_code == 0, res.output
    res = _call("models", "add", "--provider", "fake", "--model", "fake-1", "--name", "f1")
    assert res.exit_code == 0, res.output
    res = _call("provider", "use", "fake")
    assert res.exit_code == 0, res.output
    res = _call("model", "use", "f1")
    assert res.exit_code == 0, res.output


# --- profiles ---------------------------------------------------------------


def test_profiles_lifecycle(env):
    res = _call("profiles", "list")
    assert res.exit_code == 0
    assert "workspace" in res.output
    assert "(builtin)" in res.output

    res = _call("profiles", "create", "strict")
    assert res.exit_code == 0

    res = _call("profiles", "use", "strict")
    assert res.exit_code == 0
    res = _call("config", "get", "profile")
    assert res.output.strip() == "strict"

    res = _call("profiles", "clone", "strict", "stricter")
    assert res.exit_code == 0
    res = _call("profiles", "show", "stricter")
    assert res.exit_code == 0
    assert "stricter" in res.output

    # the active profile cannot be removed
    res = _call("profiles", "remove", "strict")
    assert res.exit_code != 0

    res = _call("profiles", "use", "workspace")
    assert res.exit_code == 0
    res = _call("profiles", "remove", "strict")
    assert res.exit_code == 0
    # builtins cannot be removed
    res = _call("profiles", "remove", "workspace")
    assert res.exit_code != 0


# --- project ----------------------------------------------------------------


def test_project_detection_and_registry(env):
    _tmp, work = env
    res = _call("project", "current")
    assert "no project" in res.output

    _git_init(work)
    res = _call("project", "current")
    assert str(work) in res.output

    res = _call("project", "add", str(work))
    assert res.exit_code == 0
    res = _call("project", "list")
    assert str(work) in res.output

    res = _call("project", "remove", str(work))
    assert res.exit_code == 0
    res = _call("project", "list")
    assert "no registered projects" in res.output

    res = _call("project", "doctor")
    assert res.exit_code == 0


def test_project_instructions_on_clean_tree(env):
    _git_init(env[1])
    res = _call("project", "instructions")
    assert res.exit_code == 0


# --- permissions ------------------------------------------------------------


def test_permissions_check_and_show(env):
    _git_init(env[1])
    res = _call("--json", "permissions", "show")
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["ok"] is True
    assert data["data"]["kind"] == "PROJECT"

    res = _call("--json", "permissions", "check", "fs.read")
    assert res.exit_code == 0
    assert json.loads(res.output)["data"]["action"] == "allow"

    res = _call("--json", "permissions", "check", "fs.write", "--path", str(env[1] / "a.txt"))
    assert json.loads(res.output)["data"]["action"] == "allow"

    res = _call(
        "--json",
        "permissions",
        "check",
        "fs.write",
        "--path",
        str(env[0] / "outside.txt"),
    )
    # outside the project root: workspace profile does not allow
    assert json.loads(res.output)["data"]["action"] in ("deny", "ask")

    res = _call("permissions", "explain", "network.outbound")
    assert res.exit_code == 0

    res = _call("permissions", "grants")
    assert res.exit_code == 0
    assert "no persistent grants" in res.output


# --- checkpoint -------------------------------------------------------------


def test_checkpoint_full_flow(env):
    work = _git_init(env[1])
    _provider()
    res = _call("--json", "session", "new")
    assert res.exit_code == 0, res.output
    record = json.loads(res.output)["data"]
    assert record["kind"] == "PROJECT"

    (work / "b.txt").write_text("b\n")
    res = _call("--json", "checkpoint", "create", "--label", "wip")
    assert res.exit_code == 0, res.output
    created = json.loads(res.output)["data"]
    assert created["agent_changes"] >= 1

    res = _call("--json", "checkpoint", "list")
    assert json.loads(res.output)["data"][0]["id"] == created["id"]

    res = _call("--json", "checkpoint", "show")
    assert json.loads(res.output)["data"]["id"] == created["id"]

    res = _call("--json", "checkpoint", "restore", created["id"])
    assert res.exit_code == 0

    res = _call("--json", "checkpoint", "remove", created["id"])
    assert res.exit_code == 0
    res = _call("--json", "checkpoint", "remove", created["id"])
    assert res.exit_code != 0


# --- sessions export/import -------------------------------------------------


def test_session_export_import_roundtrip(env):
    env[1]
    _provider()
    res = _call("session", "new", "--chat")
    assert res.exit_code == 0, res.output
    assert "CHAT" in res.output

    res = _call("export")  # most recent session
    assert res.exit_code == 0, res.output
    document = json.loads(res.output)
    assert document["version"] == "1"
    session_id = document["session"]["id"]

    out = env[0] / "sess.json"
    res = _call("export", "--out", str(out))
    assert res.exit_code == 0
    assert out.is_file()

    res = _call("import", str(out))
    assert res.exit_code == 0, res.output
    imported = res.output.strip().split()[1]
    assert imported != session_id  # new id assigned on import

    res = _call("session", "list")
    assert res.exit_code == 0
    assert session_id in res.output
    assert imported in res.output


def test_session_rename_and_stop(env):
    _provider()
    res = _call("session", "new", "--name", "alpha")
    assert res.exit_code == 0
    session_id = res.output.strip().split()[1]

    res = _call("session", "rename", session_id, "beta")
    assert res.exit_code == 0
    res = _call("session", "show", session_id)
    assert "beta" in res.output

    res = _call("stop")
    assert res.exit_code == 0

    res = _call("session", "stop", session_id)
    assert res.exit_code == 0
    res = _call("session", "show", session_id)
    assert "stopped" in res.output


# --- agents -----------------------------------------------------------------


def test_agents_definitions(env):
    res = _call("agents", "available")
    assert res.exit_code == 0
    assert "explore" in res.output
    assert "implementer" in res.output

    res = _call("agents", "list")
    assert res.exit_code == 0

    res = _call("agents", "show", "explore")
    assert res.exit_code == 0
    assert "read-only" in res.output

    res = _call("agents", "validate")
    assert res.exit_code == 0
    assert "OK:" in res.output

    # scaffold a project agent
    res = _call("agents", "create", "qa")
    assert res.exit_code == 0
    assert (env[1] / ".rinari" / "agents" / "qa.md").is_file()

    # built-in names require --force
    res = _call("agents", "create", "explore")
    assert res.exit_code != 0
    res = _call("agents", "create", "explore", "--force")
    assert res.exit_code == 0

    res = _call("agents", "stop", "whatever")
    assert res.exit_code != 0
    assert "interactive session" in res.output


def test_agents_project_agents_are_trust_gated(env):
    work = _git_init(env[1])
    res = _call("agents", "create", "qa")
    assert res.exit_code == 0
    (work / ".rinari" / "agents" / "extra.md").write_text(
        "---\nname: extra\ndescription: x\nprofile: read-only\n---\n"
    )
    # untrusted projects must not expose project agents
    res = _call("agents", "list")
    assert res.exit_code == 0
    assert "qa" not in res.output

    res = _call("trust", "add", str(work))
    assert res.exit_code == 0, res.output

    res = _call("agents", "list")
    assert res.exit_code == 0
    assert "qa" in res.output
    assert "extra" in res.output


# --- tools ------------------------------------------------------------------


def test_tools_inspection(env):
    res = _call("tools", "list")
    assert res.exit_code == 0
    assert "fs.read" in res.output

    res = _call("tools", "search", "git")
    assert res.exit_code == 0
    assert "git.branch" in res.output

    res = _call("tools", "show", "fs.read")
    assert res.exit_code == 0
    assert "low" in res.output

    res = _call("tools", "permissions", "fs.write")
    assert res.exit_code == 0

    res = _call("tools", "doctor")
    assert res.exit_code == 0
    assert "OK" in res.output


# --- traces / logs / metrics -------------------------------------------------


def test_trace_logs_metrics_on_session(env):
    _provider()
    res = _call("--json", "session", "new", "--chat")
    assert res.exit_code == 0
    session_id = json.loads(res.output)["data"]["id"]

    res = _call("--json", "trace")
    assert res.exit_code == 0
    data = json.loads(res.output)["data"]
    assert data["session"] == session_id

    res = _call("--json", "logs", "tail")
    assert res.exit_code == 0

    res = _call("--json", "logs", "search", "Session")
    assert res.exit_code == 0

    res = _call("--json", "metrics")
    assert res.exit_code == 0
    metrics = json.loads(res.output)["data"]
    assert "sessions" in metrics
    assert "model" in metrics


# --- secrets ----------------------------------------------------------------


def test_secrets_never_echo_values(env):
    res = _call("secrets", "list")
    assert res.exit_code == 0
    assert "no providers with secrets" in res.output

    _provider()
    res = _call("--json", "secrets", "list")
    assert res.exit_code == 0
    rows = json.loads(res.output)["data"]
    assert rows, "expected one provider secret"
    assert "sk-fake-test" not in res.output


# --- sandbox ----------------------------------------------------------------


def test_sandbox_status_and_test(env):
    (env[1] / "a.txt").write_text("a\n")
    res = _call("--json", "sandbox", "status")
    assert res.exit_code == 0
    data = json.loads(res.output)["data"]
    assert data["profile"] in ("workspace", "read-only", "full-access")
    assert data["home_locked"] is True

    res = _call("--json", "sandbox", "test", str(env[1] / "a.txt"), "--mode", "read")
    assert json.loads(res.output)["data"]["allowed"] is True

    res = _call("--json", "sandbox", "show", str(env[1] / "a.txt"))
    assert json.loads(res.output)["data"]["read"] is True

    res = _call("sandbox", "profiles")
    assert res.exit_code == 0
    assert "read-only" in res.output

    # sandbox exec runs a bounded command in the sandbox workdir
    import sys

    res = _call("sandbox", "exec", "--yes", "--", sys.executable, "-c", "print('inside')")
    assert res.exit_code == 0, res.output
    assert "inside" in res.output


# --- update -----------------------------------------------------------------


def test_update_with_mocked_client(env, monkeypatch):
    from rinari.cli.commands import update_cmd

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"info": {"version": "99.0.0"}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, url, timeout=None):
            return _Resp()

        def close(self):
            pass

    monkeypatch.setattr(update_cmd.httpx, "Client", FakeClient)
    res = _call("--json", "update", "--check")
    assert res.exit_code == 1  # new version available -> exit 1 with --check
    data = json.loads(res.output)["data"]
    assert data["update_available"] is True
    assert data["latest"] == "99.0.0"
