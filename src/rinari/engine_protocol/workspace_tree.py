"""The Files view's tree (plan 0.4.0 #98): one folder per call.

`workspace.dir.list` lists a folder of the conversation's workspace: folders
first, then files, honouring `.gitignore` when the root is a git repository
and always skipping dependency and build folders. Each entry carries its git
state (a folder carries the state of what changed inside it), so the tree
can colour it like Claude Code's. Never leaves the root.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError

ALWAYS_SKIPPED = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".rinari-worktrees",
        ".DS_Store",
        "Thumbs.db",
    }
)
LIMIT = 500


def register_workspace_tree(dispatcher: Any, services: Any) -> None:
    dispatcher.register("workspace.dir.list", lambda params: dir_list(services, params))


def _root_of(services: Any, session_id: str) -> Path:
    record = services.sessions.show(session_id)
    return Path(record.project_root_snapshot or record.current_cwd).resolve()


def _git(root: Path, *args: str, stdin: bytes | None = None) -> bytes | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            input=stdin,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode in (0, 1) else None


def _git_root(root: Path) -> Path | None:
    out = _git(root, "rev-parse", "--show-toplevel")
    if not out:
        return None
    try:
        return Path(out.decode("utf-8").strip()).resolve()
    except (UnicodeDecodeError, OSError):
        return None


def _ignored(root: Path, relative: list[str]) -> set[str]:
    if not relative:
        return set()
    out = _git(root, "check-ignore", "-z", "--stdin", stdin="\0".join(relative).encode("utf-8"))
    if not out:
        return set()
    return {item for item in out.decode("utf-8", "replace").split("\0") if item}


def _status(root: Path, folder: str) -> dict[str, str]:
    """`{path relative to root: letter}` for what changed under `folder`."""
    out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", folder or ".")
    if not out:
        return {}
    states: dict[str, str] = {}
    items = out.decode("utf-8", "replace").split("\0")
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        if len(item) < 4:
            continue
        code, path = item[:2], item[3:]
        if code[0] == "R" or code[0] == "C":
            index += 1  # the original path follows a rename
        letter = "U" if code == "??" else ("D" if "D" in code else "A" if "A" in code else "M")
        states[path.rstrip("/")] = letter
    return states


def dir_list(services: Any, params: dict[str, Any]) -> dict[str, Any]:
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise EngineProtocolError(INVALID_PARAMS, "Param 'session_id' is required.")
    wanted = params.get("path") or ""
    if not isinstance(wanted, str):
        raise EngineProtocolError(INVALID_PARAMS, "Param 'path' must be a string.")
    root = _root_of(services, session_id)
    folder = (root / wanted).resolve()
    if folder != root and root not in folder.parents:
        raise EngineProtocolError(INVALID_PARAMS, "Param 'path' is outside the workspace.")
    if not folder.is_dir():
        raise EngineProtocolError(INVALID_PARAMS, f"Not a folder: {wanted}")
    try:
        children = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        raise EngineProtocolError(INVALID_PARAMS, f"Cannot read {wanted}: {exc}") from exc
    children = [child for child in children if child.name not in ALWAYS_SKIPPED]
    repo = _git_root(root)
    relative_to_root = {child: child.relative_to(root).as_posix() for child in children}
    ignored: set[str] = set()
    states: dict[str, str] = {}
    if repo is not None:
        to_repo = {child: child.relative_to(repo).as_posix() for child in children}
        ignored_repo = _ignored(repo, list(to_repo.values()))
        ignored = {relative_to_root[c] for c, rel in to_repo.items() if rel in ignored_repo}
        prefix = root.relative_to(repo).as_posix() if root != repo else ""
        for path, letter in _status(repo, folder.relative_to(repo).as_posix()).items():
            local = path[len(prefix) + 1 :] if prefix and path.startswith(prefix + "/") else path
            states[local] = letter
    entries: list[dict[str, Any]] = []
    for child in children:
        relative = relative_to_root[child]
        if relative in ignored:
            continue
        is_dir = child.is_dir()
        entry: dict[str, Any] = {
            "name": child.name,
            "relative_path": relative,
            "path": str(child),
            "kind": "dir" if is_dir else "file",
        }
        if is_dir:
            inside = [letter for path, letter in states.items() if path.startswith(relative + "/")]
            if inside:
                entry["git"] = "M" if any(letter != "U" for letter in inside) else "U"
        elif relative in states:
            entry["git"] = states[relative]
        entries.append(entry)
        if len(entries) >= LIMIT:
            break
    return {
        "root": str(root),
        "path": "" if folder == root else folder.relative_to(root).as_posix(),
        "entries": entries,
        "truncated": len(entries) >= LIMIT,
        "git": repo is not None,
    }
