"""Project working-tree reads for desktop clients.

Thin, read-only git surface: per-file status and unified diffs with explicit
truncation. Anything that mutates the tree (restore, checkout) stays in the
owning services; this module never writes.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rinari.projects._git_process import run_git


@dataclass(frozen=True, slots=True)
class TreeStatus:
    available: bool
    branch: str | None = None
    head: str | None = None
    dirty: bool = False
    files: tuple[dict[str, Any], ...] = ()
    detached: bool = False
    ahead: int = 0
    behind: int = 0
    error: dict[str, Any] | None = None


def _error(code: str, message: str, *, retryable: bool) -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": retryable}


def git_files(root: Path) -> TreeStatus:
    """Parse `git status --porcelain` into stable per-file entries."""
    if not (root / ".git").exists():
        return TreeStatus(available=False)
    deadline = time.monotonic() + 3.0
    head_result = run_git(
        root,
        ["rev-parse", "HEAD"],
        timeout_s=max(0.01, deadline - time.monotonic()),
    )
    if head_result.timed_out:
        return TreeStatus(
            available=True,
            error=_error("GIT_TIMEOUT", head_result.error or "Git HEAD timed out.", retryable=True),
        )
    if head_result.returncode != 0:
        return TreeStatus(
            available=False,
            error=_error(
                "GIT_UNAVAILABLE",
                head_result.error or "Git HEAD is unavailable.",
                retryable=True,
            ),
        )
    head = head_result.stdout.strip()
    status_result = run_git(
        root,
        ["status", "--porcelain=v1", "--branch"],
        timeout_s=max(0.01, deadline - time.monotonic()),
    )
    if status_result.timed_out:
        return TreeStatus(
            available=True,
            head=head or None,
            error=_error(
                "GIT_TIMEOUT",
                status_result.error or "Git status timed out.",
                retryable=True,
            ),
        )
    if status_result.returncode != 0:
        return TreeStatus(
            available=False,
            head=head or None,
            error=_error(
                "GIT_UNAVAILABLE",
                status_result.error or "Git status is unavailable.",
                retryable=True,
            ),
        )
    porcelain = status_result.stdout
    if not head:
        return TreeStatus(
            available=False,
            error=_error("GIT_UNAVAILABLE", "Git HEAD is unavailable.", retryable=True),
        )
    lines = porcelain.splitlines()
    header = lines[0][3:] if lines and lines[0].startswith("## ") else ""
    detached = header.startswith("HEAD (no branch)") or header.startswith("HEAD (detached")
    branch = None if detached else header.split("...", 1)[0].split(" ", 1)[0] or None
    ahead_match = re.search(r"ahead (\d+)", header)
    behind_match = re.search(r"behind (\d+)", header)
    files: list[dict[str, Any]] = []
    for line in lines[1:] if lines and lines[0].startswith("## ") else lines:
        if len(line) < 4:
            continue
        staged, unstaged, rest = line[0], line[1], line[3:]
        # Renames print as "R  old -> new"; the working path is after the arrow.
        path = rest.split(" -> ")[-1].strip().strip('"')
        if not path:
            continue
        files.append(
            {
                "path": path,
                "staged": staged if staged != " " else None,
                "unstaged": unstaged if unstaged != " " else None,
            }
        )
    files.sort(key=lambda item: item["path"])
    return TreeStatus(
        available=True,
        branch=branch,
        head=head,
        dirty=bool(files),
        files=tuple(files),
        detached=detached,
        ahead=int(ahead_match.group(1)) if ahead_match else 0,
        behind=int(behind_match.group(1)) if behind_match else 0,
    )


def git_diff(root: Path, file: str | None = None, max_chars: int = 200_000) -> dict[str, Any]:
    """Unified diff for the tree (or one file). Truncated, never binary soup."""
    args = ["diff", "--no-color", "--no-ext-diff"]
    if file:
        args += ["--", file]
    try:
        result = run_git(root, args, timeout_s=3.0)
        if result.returncode != 0 or result.timed_out:
            raise InvalidGitError(result.error or "Git diff failed.")
        raw = result.stdout
    except OSError as exc:
        raise InvalidGitError(str(exc)) from exc
    if "\0" in raw:
        return {"diff": "", "truncated": True, "binary": True, "chars": 0}
    if len(raw) > max_chars:
        return {"diff": raw[:max_chars], "truncated": True, "binary": False, "chars": len(raw)}
    return {"diff": raw, "truncated": False, "binary": False, "chars": len(raw)}


class InvalidGitError(Exception):
    """git is missing, the directory is not a repo, or diff failed."""
