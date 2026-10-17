"""workspace.dir.list (plan 0.4.0 #98): the Files view's tree, one folder per call."""

from __future__ import annotations

import subprocess

from . import test_engine_turns as base
from .test_engine_turns import _req

services = base.services
server = base.server


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _session(server, root):
    created = server.handle_line(_req("c", "session.create", {"cwd": str(root)}))
    assert created["ok"], created
    return created["result"]["session"]["id"]


def _list(server, session_id, path="", tag="l"):
    reply = server.handle_line(
        _req(tag, "workspace.dir.list", {"session_id": session_id, "path": path})
    )
    return reply


def test_a_folder_lists_folders_first_without_ignored_and_with_git_state(server, tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "node_modules" / "x").mkdir(parents=True)
    (repo / "dist").mkdir()
    (repo / "src" / "app.ts").write_text("a", encoding="utf-8")
    (repo / "README.md").write_text("r", encoding="utf-8")
    (repo / ".gitignore").write_text("dist/\n*.log\n", encoding="utf-8")
    (repo / "debug.log").write_text("l", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    (repo / "src" / "app.ts").write_text("changed", encoding="utf-8")
    (repo / "src" / "new.ts").write_text("n", encoding="utf-8")
    session_id = _session(server, repo)

    top = _list(server, session_id)
    assert top["ok"], top
    names = [entry["name"] for entry in top["result"]["entries"]]
    assert names == ["src", ".gitignore", "README.md"]
    src = top["result"]["entries"][0]
    assert src["kind"] == "dir" and src["git"] == "M"
    assert top["result"]["git"] is True

    inside = _list(server, session_id, "src", tag="l2")["result"]["entries"]
    assert {entry["name"]: entry.get("git") for entry in inside} == {"app.ts": "M", "new.ts": "U"}
    assert inside[0]["relative_path"] == "src/app.ts"


def test_it_never_leaves_the_root(server, tmp_path):
    root = tmp_path / "chat"
    root.mkdir()
    (root / "notes.txt").write_text("n", encoding="utf-8")
    session_id = _session(server, root)
    listed = _list(server, session_id)
    assert [entry["name"] for entry in listed["result"]["entries"]] == ["notes.txt"]
    assert listed["result"]["git"] is False
    outside = _list(server, session_id, "..", tag="o")
    assert not outside["ok"] and "outside" in outside["error"]["message"]
