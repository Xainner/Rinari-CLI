"""`rinari bundles`: list, activate, create and move work between Rinari profiles."""

from __future__ import annotations

import json

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
    _ok("providers", "add", "openai", "--name", "fake", "--api-key", "sk-fake-test")
    _ok("models", "add", "--provider", "fake", "--model", "fake-1", "--name", "f1")
    _ok("provider", "use", "fake")
    _ok("model", "use", "f1")
    return tmp_path, work


def _call(*args):
    return runner.invoke(app, list(args), catch_exceptions=False)


def _ok(*args):
    res = _call(*args)
    assert res.exit_code == 0, res.output
    return res


def _data(*args):
    payload = json.loads(_ok("--json", *args).output)
    assert payload["ok"] is True, payload
    return payload["data"]


def test_create_activate_and_list(env):
    listed = _data("bundles", "list")
    assert listed["active_id"] == "default"
    assert [p["id"] for p in listed["profiles"]] == ["default"]

    created = _data("bundles", "create", "trabajo", "--name", "Trabajo", "--agent", "explore=f1")
    assert created["profile"]["agents"] == {"explore": {"model": "f1"}}
    assert created["activated"] is False

    _data("bundles", "activate", "trabajo")
    _data("session", "new", "--chat")
    listed = _data("bundles", "list")
    by_id = {p["id"]: p for p in listed["profiles"]}
    assert listed["active_id"] == "trabajo"
    assert by_id["trabajo"]["active"] and not by_id["default"]["active"]
    assert by_id["trabajo"]["sessions"] == 1

    text = _ok("bundles", "list").output
    assert "* trabajo" in text and "  default" in text


def test_create_rejects_unknown_agent_or_model(env):
    assert _call("bundles", "create", "x", "--name", "X", "--agent", "nope=f1").exit_code != 0
    assert _call("bundles", "create", "x", "--name", "X", "--agent", "explore=ghost").exit_code != 0
    assert _call("bundles", "create", "x", "--name", "X", "--agent", "explore").exit_code != 0
    assert [p["id"] for p in _data("bundles", "list")["profiles"]] == ["default"]


def test_move_project_and_sessions(env):
    _tmp, work = env
    _data("bundles", "create", "casa", "--name", "Casa")
    project = _data("project", "add", str(work))
    in_project = _data("session", "new")
    assert in_project["project_id"] == project["id"]
    loose = _data("session", "new", "--chat")

    # A conversation inside a project needs the whole project to move.
    refused = _call("bundles", "move-session", in_project["id"], "casa")
    assert refused.exit_code != 0
    moved = _data("bundles", "move-session", in_project["id"], "casa", "--with-project")
    assert moved["project_id"] == project["id"]
    assert moved["session_ids"] == [in_project["id"]]

    moved = _data("bundles", "move-session", loose["id"], "casa")
    assert (moved["previous_rinari_profile_id"], moved["rinari_profile_id"]) == (
        "default",
        "casa",
    )

    # Back by path: any folder of the project names it.
    back = _data("bundles", "move-project", str(work), "default")
    assert back["project_id"] == project["id"]
    assert back["previous_rinari_profile_id"] == "casa"
    counts = {p["id"]: p for p in _data("bundles", "list")["profiles"]}
    assert (counts["default"]["projects"], counts["casa"]["projects"]) == (1, 0)
    assert counts["casa"]["sessions"] == 1

    assert _call("bundles", "move-project", "no-such-project", "casa").exit_code != 0
    assert _call("bundles", "move-project", project["id"], "ghost").exit_code != 0
