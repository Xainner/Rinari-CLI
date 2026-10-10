"""The current branch of a folder, read from `.git/HEAD` without running git.

Project lists show each project's branch, and they are refreshed often: one
`git` subprocess per project per refresh is too much. HEAD is a tiny file,
so reading it (and caching by mtime) costs two `stat` calls per project.

Handles a project nested in a repository (walks up), worktrees and
submodules (`.git` is a file with `gitdir: <path>`), a detached HEAD and a
rebase in progress (HEAD is detached, the branch lives in
`rebase-merge/head-name`). Anything unexpected gives an empty result, never
an error: a missing branch label is not worth failing a list for.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

_MAX_LEVELS = 25
_MAX_HEAD_BYTES = 256
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_REF = "ref: refs/heads/"


@dataclass(frozen=True, slots=True)
class GitHead:
    branch: str | None = None
    detached: bool = False
    sha_short: str | None = None
    operation: str | None = None  # "rebase" while one is in progress

    def as_dict(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "detached": self.detached,
            "sha_short": self.sha_short,
            "operation": self.operation,
        }


def find_git_dir(root: Path) -> Path | None:
    """The git directory for `root` or its nearest ancestor repository."""
    try:
        current = root.resolve()
    except (OSError, RuntimeError):
        return None
    for _ in range(_MAX_LEVELS):
        dot_git = current / ".git"
        try:
            if dot_git.is_dir():
                return dot_git
            if dot_git.is_file():
                return _gitdir_from_file(dot_git)
        except OSError:
            return None
        if current.parent == current:
            return None
        current = current.parent
    return None


def _gitdir_from_file(dot_git: Path) -> Path | None:
    try:
        text = dot_git.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("gitdir:"):
            target = Path(line[len("gitdir:") :].strip())
            if not target.is_absolute():
                target = dot_git.parent / target
            try:
                resolved = target.resolve()
            except (OSError, RuntimeError):
                return None
            return resolved if resolved.is_dir() else None
    return None


def _read_small(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            return handle.read(_MAX_HEAD_BYTES).decode("utf-8", errors="replace").strip()
    except OSError:
        return None


def _branch_name(ref: str) -> str | None:
    ref = ref.strip()
    if ref.startswith("refs/heads/"):
        ref = ref[len("refs/heads/") :]
    return ref or None


def read_head(root: Path) -> GitHead:
    git_dir = find_git_dir(root)
    if git_dir is None:
        return GitHead()
    return _parse_head(git_dir)


def _parse_head(git_dir: Path) -> GitHead:
    content = _read_small(git_dir / "HEAD")
    if not content:
        return GitHead()
    if content.startswith(_REF):
        return GitHead(branch=_branch_name(content[len("ref: ") :]))
    if not _SHA.fullmatch(content):
        return GitHead()
    for marker in ("rebase-merge/head-name", "rebase-apply/head-name"):
        head_name = _read_small(git_dir / marker)
        if head_name:
            return GitHead(
                branch=_branch_name(head_name),
                detached=True,
                sha_short=content[:7],
                operation="rebase",
            )
    return GitHead(detached=True, sha_short=content[:7])


class HeadCache:
    """Per-root HEAD, revalidated by the HEAD file's mtime and size."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[Path | None, int, int, GitHead]] = {}

    def get(self, root: str | Path) -> GitHead:
        key = str(root)
        with self._lock:
            entry = self._entries.get(key)
        if entry is not None:
            git_dir, mtime, size, head = entry
            if git_dir is None:
                # Not a repository last time: look again only if `.git` appeared.
                if not (Path(key) / ".git").exists():
                    return head
            else:
                stamp = _stamp(git_dir)
                if stamp == (mtime, size):
                    return head
        git_dir = find_git_dir(Path(key))
        head = _parse_head(git_dir) if git_dir is not None else GitHead()
        mtime, size = _stamp(git_dir) if git_dir is not None else (0, 0)
        with self._lock:
            self._entries[key] = (git_dir, mtime, size, head)
        return head


def _stamp(git_dir: Path) -> tuple[int, int]:
    """HEAD's mtime and size, plus a rebase marker so starting one is seen."""
    try:
        stat = (git_dir / "HEAD").stat()
    except OSError:
        return (0, 0)
    rebasing = (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()
    return (stat.st_mtime_ns, stat.st_size + (1 << 40 if rebasing else 0))


#: Shared by the project views of one Engine process.
HEADS = HeadCache()
