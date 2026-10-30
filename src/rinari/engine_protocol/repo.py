"""The conversation's repository for the «Resumen» panel (plan 0.4.0 #82).

`local_status` is read-only and quick (bounded git probes): branch, upstream,
ahead/behind, dirty files and the remote with its credentials stripped.
`remote_info` asks the GitHub CLI whether it is signed in and for the pull
requests of the branch with their checks; it can take seconds, so the server
runs it off the request loop and announces the result with an event.
`pr_create` publishes a pull request with `gh`; the desktop confirms before.
Nothing here pushes, commits or changes the tree.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from rinari.engine_protocol.workspace import git_files
from rinari.projects._git_process import run_git, run_process

GH_TIMEOUT_S = 10.0
REMOTE_TTL_S = 60.0
_GH_ENV = {"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
_remote_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()

_GITHUB = (
    re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"),
    re.compile(
        r"^(?:https?|ssh|git)://(?:[^@/]+@)?github\.com(?::\d+)?/(?P<owner>[^/]+)/"
        r"(?P<repo>[^/]+?)(?:\.git)?/?$"
    ),
)
_FAILED = ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE")
_PENDING = ("", "PENDING", "EXPECTED", "IN_PROGRESS", "QUEUED", "WAITING", "REQUESTED")


class RepoError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _git(root: Path, *args: str) -> str | None:
    result = run_git(root, list(args), timeout_s=3.0)
    if result.returncode != 0 or result.timed_out:
        return None
    return result.stdout.strip() or None


def safe_remote(url: str | None) -> str | None:
    """The remote without user, password or token (`https://x:tok@host/...`)."""
    if not url:
        return None
    if "://" not in url:
        return url  # scp-like `git@github.com:owner/repo.git` carries no secret
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    if parts.scheme in ("http", "https"):
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    user = f"{parts.username}@" if parts.username and not parts.password else ""
    return urlunsplit((parts.scheme, f"{user}{host}", parts.path, "", ""))


def github_of(url: str | None) -> dict[str, str] | None:
    if not url:
        return None
    for pattern in _GITHUB:
        match = pattern.match(url.strip())
        if match:
            return {"owner": match["owner"], "repo": match["repo"]}
    return None


def _unavailable(root: Path | None) -> dict[str, Any]:
    return {
        "available": False,
        "root": str(root) if root else None,
        "branch": None,
        "upstream": None,
        "ahead": 0,
        "behind": 0,
        "dirty": False,
        "changed": 0,
        "remote_url": None,
        "github": None,
    }


def local_status(cwd: Path | None) -> dict[str, Any]:
    if cwd is None or not cwd.is_dir():
        return _unavailable(cwd)
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top is None:
        return _unavailable(cwd)
    root = Path(top)
    tree = git_files(root)
    if not tree.available:
        return _unavailable(root)
    upstream = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    remote_name = upstream.split("/", 1)[0] if upstream else "origin"
    remote_url = safe_remote(_git(root, "remote", "get-url", remote_name))
    return {
        "available": True,
        "root": str(root),
        "branch": tree.branch,
        "upstream": upstream,
        "ahead": tree.ahead,
        "behind": tree.behind,
        "dirty": tree.dirty,
        "changed": len(tree.files),
        "remote_url": remote_url,
        "github": github_of(remote_url),
    }


def remote_key(status: dict[str, Any]) -> tuple[str, str] | None:
    """What the GitHub part depends on; None when there is nothing to ask."""
    if not status.get("available") or not status.get("github"):
        return None
    return (str(status["root"]), str(status.get("branch") or ""))


def cached_remote(key: tuple[str, str]) -> tuple[dict[str, Any] | None, bool]:
    """(cached GitHub part, still fresh)."""
    with _cache_lock:
        entry = _remote_cache.get(key)
    if entry is None:
        return None, False
    return entry[1], time.monotonic() - entry[0] < REMOTE_TTL_S


def forget_remote(key: tuple[str, str]) -> None:
    with _cache_lock:
        _remote_cache.pop(key, None)


def gh_state(cwd: Path) -> str:
    """`missing`, `unauthenticated` or `ready`."""
    executable = shutil.which("gh")
    if executable is None:
        return "missing"
    result = run_process(
        [executable, "auth", "status", "--hostname", "github.com"],
        cwd,
        timeout_s=GH_TIMEOUT_S,
        env=_GH_ENV,
        label="gh auth status",
    )
    return "ready" if result.returncode == 0 else "unauthenticated"


def checks_of(rollup: Any) -> str | None:
    if not isinstance(rollup, list) or not rollup:
        return None
    outcomes = []
    for item in rollup:
        if not isinstance(item, dict):
            continue
        value = str(item.get("conclusion") or item.get("state") or "").upper()
        status = str(item.get("status") or "").upper()
        if status and status != "COMPLETED" and not item.get("conclusion"):
            value = "PENDING"
        outcomes.append(value)
    if any(value in _FAILED for value in outcomes):
        return "failure"
    if any(value in _PENDING for value in outcomes):
        return "pending"
    return "success"


def _pull_requests(root: Path, branch: str) -> list[dict[str, Any]]:
    executable = shutil.which("gh")
    if executable is None or not branch:
        return []
    result = run_process(
        [
            executable,
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "10",
            "--json",
            "number,title,state,url,statusCheckRollup",
        ],
        root,
        timeout_s=GH_TIMEOUT_S,
        env=_GH_ENV,
        label="gh pr list",
    )
    if result.returncode != 0 or result.timed_out:
        return []
    try:
        rows = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    return [
        {
            "number": row.get("number"),
            "title": row.get("title") or "",
            "state": str(row.get("state") or "").lower(),
            "url": row.get("url") or "",
            "checks": checks_of(row.get("statusCheckRollup")),
        }
        for row in rows
        if isinstance(row, dict)
    ]


def remote_info(key: tuple[str, str]) -> dict[str, Any]:
    """The slow GitHub part (`gh` sign-in and the branch's pull requests); cached."""
    root, branch = Path(key[0]), key[1]
    gh = gh_state(root)
    info = {"gh": gh, "prs": _pull_requests(root, branch) if gh == "ready" else []}
    with _cache_lock:
        _remote_cache[key] = (time.monotonic(), info)
    return info


def check_pr(status: dict[str, Any], base: str) -> None:
    """What makes a pull request impossible, known without asking GitHub."""
    if not status["available"]:
        raise RepoError("REPO_UNAVAILABLE", "This conversation has no git repository.")
    if not status["github"]:
        raise RepoError("NOT_GITHUB", "The remote is not on GitHub.")
    branch = status["branch"]
    if not branch:
        raise RepoError("DETACHED_HEAD", "There is no branch to open a pull request from.")
    if branch == base:
        raise RepoError("SAME_BRANCH", f"The branch is already {base}.")
    if not status["upstream"]:
        raise RepoError("NOT_PUSHED", f"Push {branch} before opening a pull request.")
    if status["ahead"]:
        raise RepoError(
            "UNPUSHED_COMMITS",
            f"{branch} has {status['ahead']} commit(s) that are not pushed.",
            ahead=status["ahead"],
        )


def pr_create(status: dict[str, Any], title: str, base: str, body: str) -> dict[str, Any]:
    """Open the pull request with `gh` (slow: run it off the request loop)."""
    check_pr(status, base)
    root = Path(status["root"])
    gh = gh_state(root)
    executable = shutil.which("gh")
    if gh != "ready" or executable is None:
        raise RepoError("GH_NOT_READY", "The GitHub CLI is not installed or not signed in.", gh=gh)
    upstream = str(status["upstream"])
    head = upstream.split("/", 1)[1] if "/" in upstream else str(status["branch"])
    result = run_process(
        [
            executable,
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--base",
            base,
            "--head",
            head,
        ],
        root,
        timeout_s=30.0,
        env=_GH_ENV,
        label="gh pr create",
    )
    if result.returncode != 0 or result.timed_out:
        detail = (result.stderr or result.error or "gh pr create failed").strip()[:500]
        raise RepoError("PR_CREATE_FAILED", detail)
    url = next(
        (line.strip() for line in reversed(result.stdout.splitlines()) if "/pull/" in line),
        "",
    )
    number = re.search(r"/pull/(\d+)", url)
    return {"url": url, "number": int(number.group(1)) if number else None}
