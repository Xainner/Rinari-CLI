"""Git repository state for project fingerprints and status display."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rinari.projects._git_process import capture_git


@dataclass(frozen=True, slots=True)
class GitState:
    available: bool
    branch: str | None = None
    head: str | None = None
    dirty: bool = False


def git_state(root: Path) -> GitState:
    if not (root / ".git").exists():
        return GitState(available=False)
    head_output = _run(root, ["rev-parse", "HEAD"])
    if head_output is None:
        return GitState(available=False)
    head = head_output.strip()
    if not head or head == "fatal: not a git repository":
        return GitState(available=False)
    branch_output = _run(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    status_output = _run(root, ["status", "--porcelain"])
    if branch_output is None or status_output is None:
        return GitState(available=False)
    branch = branch_output.strip()
    status = status_output.strip()
    return GitState(available=True, branch=branch or None, head=head, dirty=bool(status))


def git_fingerprint(root: Path) -> str | None:
    state = git_state(root)
    if not state.available or state.head is None:
        return None
    return state.head[:12]


def _run(root: Path, args: list[str]) -> str | None:
    return capture_git(root, args, timeout_s=10.0)
