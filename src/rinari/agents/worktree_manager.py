"""WorktreeManager: isolated writer workspaces for parallel subagents (phase 6).

Parallel writers never share the main working tree (harness.md 114,
AGENTS.md section 19). Each writer runs in `git worktree` under
`<root>/.rinari-worktrees/<branch>` on branch `rinari/<agent>-<n>`; the
manager commits the writer's change, extracts the patch + changed files,
attempts the merge into the main worktree, and reports conflicts. The main
coordinator integrates; the manager also cleans up when told.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

WORKTREE_PARENT = ".rinari-worktrees"
BRANCH_PREFIX = "rinari/"


class WorktreeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        # NOT_A_REPO | WORKTREE_FAILED | MERGE_CONFLICT | WORKTREE_NOT_FOUND
        # | MERGE_FAILED
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class WorktreeInfo:
    path: Path
    branch: str


def _run_git(root: Path, args: list[str], timeout: float = 60) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:32] or "agent"


class WorktreeManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- lifecycle -------------------------------------------------------------

    def create(self, agent: str) -> WorktreeInfo:
        code, out, err = _run_git(self.root, ["rev-parse", "--is-inside-work-tree"])
        if code != 0 or not out.strip() == "true":
            raise WorktreeError("NOT_A_REPO", f"not a git repository: {self.root}")
        branch = self._next_branch(agent)
        wt_path = self.root / WORKTREE_PARENT / branch
        parent = wt_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        code, out, err = _run_git(
            self.root,
            ["worktree", "add", "-b", branch, str(wt_path)],
        )
        if code != 0:
            raise WorktreeError("WORKTREE_FAILED", f"worktree add failed: {err or out}")
        return WorktreeInfo(path=wt_path, branch=branch)

    def remove(self, info: WorktreeInfo, *, force: bool = False) -> bool:
        args = ["worktree", "remove", str(info.path)]
        if force:
            args.append("--force")
        code, out, err = _run_git(self.root, args)
        if code == 0:
            with contextlib.suppress(Exception):  # branch cleanup is best-effort
                _run_git(self.root, ["branch", "-D", info.branch])
            return True
        if "not a working tree" in (err + out).lower():
            return False
        raise WorktreeError("WORKTREE_FAILED", f"worktree remove failed: {err or out}")

    def list(self) -> list[WorktreeInfo]:
        code, out, _ = _run_git(self.root, ["worktree", "list", "--porcelain"])
        if code != 0:
            return []
        infos: list[WorktreeInfo] = []
        path = branch = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                path = Path(line[len("worktree ") :])
            elif line.startswith("branch "):
                branch = Path(line[len("branch ") :])
            elif line == "" and path is not None and branch is not None:
                infos.append(WorktreeInfo(path=path, branch=branch.name))
                path = branch = None
        return infos

    # -- results ---------------------------------------------------------------

    def changed_files(self, info: WorktreeInfo, *, against_base: bool = True) -> list[str]:
        args = ["status", "--porcelain"]
        code, out, _ = _run_git(info.path, args)
        if code != 0:
            raise WorktreeError("WORKTREE_NOT_FOUND", f"worktree missing: {info.path}")
        files: list[str] = []
        for line in out.splitlines():
            if len(line) > 3 and line[:2].strip():
                files.append(line[3:].strip().strip('"'))
        return sorted(set(files))

    def commit_result(self, info: WorktreeInfo, message: str) -> dict:
        """Stage everything in the writer worktree and commit it on its branch."""
        files = self.changed_files(info)
        if not files:
            return {"committed": False, "commit": None, "changed_files": []}
        code, out, err = _run_git(info.path, ["add", "-A"])
        if code != 0:
            raise WorktreeError("WORKTREE_FAILED", f"git add failed: {err or out}")
        code, out, err = _run_git(info.path, ["commit", "-m", message])
        if code != 0:
            raise WorktreeError("WORKTREE_FAILED", f"git commit failed: {err or out}")
        code, out, err = _run_git(info.path, ["rev-parse", "HEAD"])
        commit = out.strip() if code == 0 else None
        return {"committed": True, "commit": commit, "changed_files": files}

    def patch(self, info: WorktreeInfo) -> str:
        """The writer's committed change as a portable patch (HEAD~1..HEAD)."""
        code, out, _ = _run_git(info.path, ["diff", "--no-color", "HEAD~1"])
        if code != 0:
            code, out, _ = _run_git(info.path, ["diff", "--no-color", "HEAD"])
        return out

    def changed_files_against_base(self, info: WorktreeInfo) -> list[str]:
        code, out, _ = _run_git(info.path, ["diff", "--name-only", "--no-color", "HEAD~1"])
        if code != 0:
            return self.changed_files(info)
        return sorted({line.strip() for line in out.splitlines() if line.strip()})

    # -- integration --------------------------------------------------------------

    def try_merge(self, info: WorktreeInfo) -> dict:
        """Merge the writer branch into the main worktree; report conflicts."""
        code, out, err = _run_git(self.root, ["merge", "--no-ff", info.branch])
        if code == 0:
            return {"merged": True, "conflicts": [], "message": "merged cleanly"}
        conflicts: list[str] = []
        code2, out2, _ = _run_git(self.root, ["diff", "--name-only", "--diff-filter=U"])
        if code2 == 0:
            conflicts = sorted({line.strip() for line in out2.splitlines() if line.strip()})
        return {
            "merged": False,
            "conflicts": conflicts,
            "message": (err or out).strip() or "merge failed",
        }

    def abort_merge(self) -> bool:
        code, _, _ = _run_git(self.root, ["merge", "--abort"])
        return code == 0

    def _next_branch(self, agent: str) -> str:
        slug = _slugify(agent)
        candidates = [f"{BRANCH_PREFIX}{slug}"]
        for n in range(2, 100):
            candidates.append(f"{BRANCH_PREFIX}{slug}-{n}")
        for candidate in candidates:
            code, _, _ = _run_git(
                self.root, ["show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"]
            )
            if code != 0:
                return candidate
        raise WorktreeError("WORKTREE_FAILED", f"cannot allocate a branch for {slug!r}")


__all__ = [
    "BRANCH_PREFIX",
    "WORKTREE_PARENT",
    "WorktreeError",
    "WorktreeInfo",
    "WorktreeManager",
]
