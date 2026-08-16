"""Git repository state for project fingerprints and status display."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitState:
    available: bool
    branch: str | None = None
    head: str | None = None
    dirty: bool = False


def git_state(root: Path) -> GitState:
    if not (root / ".git").exists():
        return GitState(available=False)
    try:
        head = _run(root, ["rev-parse", "HEAD"]).strip()
        branch = _run(root, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        status = _run(root, ["status", "--porcelain"]).strip()
    except (OSError, subprocess.SubprocessError):
        return GitState(available=False)
    if not head or head == "fatal: not a git repository":
        return GitState(available=False)
    return GitState(available=True, branch=branch or None, head=head, dirty=bool(status))


def git_fingerprint(root: Path) -> str | None:
    state = git_state(root)
    if not state.available or state.head is None:
        return None
    return state.head[:12]


def _run(root: Path, args: list[str]) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout
