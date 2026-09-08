"""Project working-tree reads for desktop clients.

Thin, read-only git surface: per-file status and unified diffs with explicit
truncation. Anything that mutates the tree (restore, checkout) stays in the
owning services; this module never writes.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class TreeStatus:
    available: bool
    branch: str | None = None
    head: str | None = None
    dirty: bool = False
    files: tuple[dict[str, Any], ...] = ()


def _run(root: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise subprocess.SubprocessError(f"git {' '.join(args)} failed")
    return proc.stdout


def git_files(root: Path) -> TreeStatus:
    """Parse `git status --porcelain` into stable per-file entries."""
    if not (root / ".git").exists():
        return TreeStatus(available=False)
    try:
        head = _run(root, ["rev-parse", "HEAD"]).strip()
        branch = _run(root, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        # NOTE: no .strip() on the whole output: the leading space of a
        # " M file" line is significant (unstaged-only modification).
        porcelain = _run(root, ["status", "--porcelain"])
    except (OSError, subprocess.SubprocessError):
        return TreeStatus(available=False)
    if not head:
        return TreeStatus(available=False)
    files: list[dict[str, Any]] = []
    for line in porcelain.splitlines():
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
        branch=branch or None,
        head=head,
        dirty=bool(files),
        files=tuple(files),
    )


def git_diff(root: Path, file: str | None = None, max_chars: int = 200_000) -> dict[str, Any]:
    """Unified diff for the tree (or one file). Truncated, never binary soup."""
    args = ["diff", "--no-color", "--no-ext-diff"]
    if file:
        args += ["--", file]
    try:
        raw = _run(root, args)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InvalidGitError(str(exc)) from exc
    if "\0" in raw:
        return {"diff": "", "truncated": True, "binary": True, "chars": 0}
    if len(raw) > max_chars:
        return {"diff": raw[:max_chars], "truncated": True, "binary": False, "chars": len(raw)}
    return {"diff": raw, "truncated": False, "binary": False, "chars": len(raw)}


class InvalidGitError(Exception):
    """git is missing, the directory is not a repo, or diff failed."""
