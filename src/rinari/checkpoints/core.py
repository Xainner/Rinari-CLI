"""Checkpoint core: agent-owned change tracking and restore planning (phase 3).

A checkpoint snapshots the *current* dirty state of the repository working
tree, classifying each path by ownership against the session's worktree
baseline (captured at session start, `projects.worktree`):

    agent   dirty now, absent from the session baseline (the agent did it)
    user    dirty before the session, unchanged since (user's own work)
    mixed   dirty before the session AND changed since (both owners)

Restore semantics are deliberately conservative: a restore only rewrites
paths it can attribute to the agent. `user` paths are never touched, and
`mixed` paths are reported (mixed ownership detection) and only rewritten
when explicitly allowed -- clobbering a user's pre-session work silently is
exactly what the dirty-worktree protection exists to prevent.

The checkpoint stores, per path, the content bytes captured at create time:
`rinari undo` puts agent-owned paths back to that content.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from rinari.projects.worktree import _sha256_bounded

OWNERSHIP_AGENT = "agent"
OWNERSHIP_USER = "user"
OWNERSHIP_MIXED = "mixed"

MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
GIT_TIMEOUT_S = 10.0


@dataclass(frozen=True, slots=True)
class CheckpointEntry:
    path: str
    ownership: str
    prior_status: str
    prior_sha: str | None
    prior_bytes: bytes | None
    exists: bool

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "ownership": self.ownership,
            "prior_status": self.prior_status,
            "prior_sha": self.prior_sha,
            "exists": self.exists,
            "bytes": len(self.prior_bytes) if self.prior_bytes is not None else None,
        }


@dataclass(frozen=True, slots=True)
class RestoreOp:
    path: str
    action: str  # restore | delete | skip | noop
    reason: str
    ownership: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "action": self.action,
            "reason": self.reason,
            "ownership": self.ownership,
        }


def classify_worktree(
    root: Path, baselines: dict[str, tuple[str, str | None]]
) -> list[CheckpointEntry]:
    """Classify the current dirty tree against the session baseline.

    Returns one entry per dirty path (staged or unstaged, untracked included).
    """
    root = root.resolve()
    entries: list[CheckpointEntry] = []
    for status, path in _git_porcelain(root):
        baseline = _baseline_for(path, baselines)
        target = root / path
        sha = _sha256_bounded(target) if target.exists() else None
        if status.startswith("??"):
            ownership = OWNERSHIP_USER if baseline is not None else OWNERSHIP_AGENT
        elif baseline is None:
            ownership = OWNERSHIP_AGENT
        elif baseline[1] is None or sha is None or baseline[1] == sha:
            ownership = OWNERSHIP_USER
        else:
            ownership = OWNERSHIP_MIXED
        entries.append(
            CheckpointEntry(
                path=path,
                ownership=ownership,
                prior_status=status,
                prior_sha=sha,
                prior_bytes=_read_bounded(target) if target.exists() else None,
                exists=target.exists(),
            )
        )
    return entries


def plan_restore(entries: list[CheckpointEntry], *, allow_mixed: bool = False) -> list[RestoreOp]:
    """What a restore of this checkpoint would do, per path."""
    ops: list[RestoreOp] = []
    for entry in entries:
        if entry.ownership == OWNERSHIP_USER:
            ops.append(RestoreOp(entry.path, "skip", "user-owned (pre-session)", entry.ownership))
        elif entry.ownership == OWNERSHIP_MIXED and not allow_mixed:
            ops.append(
                RestoreOp(
                    entry.path,
                    "skip",
                    "mixed ownership (user work + in-session changes); use --allow-mixed",
                    entry.ownership,
                )
            )
        else:
            ops.append(RestoreOp(entry.path, "restore", "agent-owned", entry.ownership))
    return ops


def apply_restore(root: Path, entry: CheckpointEntry) -> str:
    """Undo one agent-owned path to its checkpoint content. Returns the action."""
    target = root / entry.path
    if entry.prior_bytes is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry.prior_bytes)
        return "restored"
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            import shutil

            shutil.rmtree(target)
        else:
            target.unlink()
        return "deleted"
    return "noop"


def _git_porcelain(root: Path) -> list[tuple[str, str]]:
    try:
        process = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    lines: list[tuple[str, str]] = []
    for line in process.stdout.splitlines():
        if not line or line.startswith("## "):
            continue
        status = line[:2].strip()
        raw = line[3:]
        if " -> " in raw and status.startswith(("R", "C")):
            raw = raw.split(" -> ", 1)[1]
        raw = raw.strip().strip('"').rstrip("/")
        if raw:
            lines.append((status, raw))
    return lines


def _baseline_for(
    path: str, baselines: dict[str, tuple[str, str | None]]
) -> tuple[str, str | None] | None:
    probe = path
    while True:
        entry = baselines.get(probe)
        if entry is not None or probe == "":
            return entry
        probe = "/".join(probe.split("/")[:-1])


def _read_bounded(path: Path) -> bytes | None:
    try:
        return path.read_bytes()[:MAX_SNAPSHOT_BYTES]
    except OSError:
        return None
