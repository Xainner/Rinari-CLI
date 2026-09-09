"""Dirty-worktree baseline and ownership guard (phase 3 protection).

At session start we snapshot the pre-existing uncommitted state of the
project working tree (git porcelain + content hashes). The guard then
classifies any path the tools touch:

    user-dirty          dirty before the session, unchanged since
    modified-in-session dirty before the session, changed meanwhile

Neither class is "the agent's own file": writing over them destroys work
the user had before the session, so `fs.write`/`fs.patch` are forced
through the approval gate and flagged in the tool result. Read-only and
new-file operations are unaffected.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from rinari.projects._git_process import capture_git

MAX_HASH_BYTES = 16 * 1024 * 1024
GIT_TIMEOUT_S = 10.0


def snapshot_worktree(root: Path) -> dict[str, tuple[str, str | None]]:
    """Capture the pre-existing dirty state of `root` (must be a git repo).

    Returns {repo-relative path: (porcelain status, sha256 or None)}.
    """
    root = root.resolve()
    output = capture_git(root, ["status", "--porcelain"], timeout_s=GIT_TIMEOUT_S)
    if output is None:
        return {}
    entries: dict[str, tuple[str, str | None]] = {}
    for line in output.splitlines():
        if not line or line.startswith("## "):
            continue
        status = line[:2].strip()
        raw_path = line[3:]
        # Renames appear as "old -> new"; own the destination.
        if " -> " in raw_path and status.startswith(("R", "C")):
            raw_path = raw_path.split(" -> ", 1)[1]
        raw_path = raw_path.strip().strip('"')
        if not raw_path:
            continue
        is_dir = raw_path.endswith("/")
        rel = raw_path.rstrip("/")
        entries[rel] = (status, None if is_dir else _sha256_bounded(root / rel))
    return entries


class WorktreeGuard:
    """Classifies paths against the session's worktree baseline."""

    def __init__(self, root: Path, baselines: dict[str, tuple[str, str | None]]) -> None:
        self.root = root.resolve()
        self._baselines = baselines

    def classify(self, path: Path) -> str | None:
        """Return 'user-dirty' | 'modified-in-session' | None (unmanaged)."""
        try:
            resolved = path.resolve()
        except OSError:
            return None
        rel = _relative_to(self.root, resolved)
        if rel is None:
            return None
        # Direct entry first; otherwise the nearest baseline ancestor
        # (writes inside a user-created untracked directory count too).
        entry: tuple[str, str | None] | None = None
        probe = rel
        while True:
            entry = self._baselines.get(probe)
            if entry is not None or probe == "":
                break
            probe = "/".join(probe.split("/")[:-1])
        if entry is None:
            return None
        status, baseline_sha = entry
        current_sha = _sha256_bounded(resolved) if resolved.exists() else None
        if (
            status.startswith(("M", "A", "R", "C", "T", "U"))
            and baseline_sha is not None
            and current_sha is not None
            and current_sha != baseline_sha
        ):
            return "modified-in-session"
        return "user-dirty"


def _relative_to(root: Path, path: Path) -> str | None:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    if str(rel) == ".":
        return None
    return rel.as_posix()


def _sha256_bounded(path: Path) -> str | None:
    """sha256 of up to MAX_HASH_BYTES (plus a size marker): change
    detection, not verification."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            digest.update(handle.read(MAX_HASH_BYTES))
    except OSError:
        return None
    if size > MAX_HASH_BYTES:
        digest.update(f"|size={size}".encode())
    return digest.hexdigest()
