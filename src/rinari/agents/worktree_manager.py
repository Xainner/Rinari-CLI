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
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rinari.projects._git_process import run_git

WORKTREE_PARENT = ".rinari-worktrees"
BRANCH_PREFIX = "rinari/"


class WorktreeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        # NOT_A_REPO | GIT_TIMEOUT | WORKTREE_FAILED | MERGE_CONFLICT
        # | WORKTREE_NOT_FOUND | MERGE_FAILED
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class WorktreeInfo:
    path: Path
    branch: str


# The exit code `_run_git` reports when git did not answer in time.
TIMED_OUT = 124


def _run_git(root: Path, args: list[str], timeout: float = 60) -> tuple[int, str, str]:
    """One bounded git call: no inherited stdin, the whole tree killed on timeout.

    A plain `subprocess.run` here once hung a child's creation for 60 s on
    Windows (the owner's demo, 2026-11-01).
    """
    try:
        result = run_git(root, args, timeout_s=timeout)
    except OSError as exc:
        return 1, "", str(exc)
    if result.timed_out:
        return TIMED_OUT, "", result.error or "git timed out"
    code = result.returncode if result.returncode is not None else 1
    return code, result.stdout, result.stderr or (result.error or "" if code else "")


def _slugify(text: str) -> str:
    # «menú» → «menu», not «men»: accents go before the rest is replaced.
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if len(slug) > 32:
        cut = slug[:32]
        slug = cut.rsplit("-", 1)[0] if "-" in cut[12:] else cut
    return slug.strip("-") or "agent"


class WorktreeManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- lifecycle -------------------------------------------------------------

    def create(self, agent: str, *, checked: bool = False) -> WorktreeInfo:
        """`checked`: the caller already knows `root` is a work tree."""
        if not checked:
            code, out, err = _run_git(self.root, ["rev-parse", "--is-inside-work-tree"], timeout=15)
            if code == TIMED_OUT:
                raise WorktreeError("GIT_TIMEOUT", f"git did not answer in {self.root}: {err}")
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
        if code == TIMED_OUT:
            raise WorktreeError("GIT_TIMEOUT", f"worktree add did not finish: {err}")
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
        # Git's own chatter (line-ending warnings, hints) is not the reason.
        reason = "\n".join(
            line
            for line in (err or out).splitlines()
            if line.strip() and not line.lower().startswith(("warning:", "hint:"))
        )
        return {
            "merged": False,
            "conflicts": conflicts,
            "message": reason.strip() or "merge failed",
        }

    def uncommitted_files(self) -> list[str]:
        """Files with changes not committed in the main worktree (its own worktrees aside)."""
        code, out, _ = _run_git(self.root, ["status", "--porcelain", "--untracked-files=all"])
        if code != 0:
            return []
        files: set[str] = set()
        for line in out.splitlines():
            if len(line) <= 3:
                continue
            path = line[3:].strip().strip('"')
            if " -> " in path:
                path = path.split(" -> ", 1)[1].strip('"')
            if not path.startswith(f"{WORKTREE_PARENT}/"):
                files.add(path)
        return sorted(files)

    def branch_files(self, branch: str, base: str | None) -> list[str]:
        """What the branch changes since it left `base` (or since it forked)."""
        spec = f"{base}..{branch}" if base else f"HEAD...{branch}"
        code, out, _ = _run_git(self.root, ["diff", "--name-only", "--no-color", spec])
        if code != 0:
            return []
        return sorted({line.strip() for line in out.splitlines() if line.strip()})

    def abort_merge(self) -> bool:
        code, _, _ = _run_git(self.root, ["merge", "--abort"])
        return code == 0

    def _next_branch(self, agent: str) -> str:
        slug = _slugify(agent)
        # One listing instead of a `show-ref` per candidate.
        code, out, err = _run_git(
            self.root,
            ["for-each-ref", "--format=%(refname:short)", f"refs/heads/{BRANCH_PREFIX}{slug}*"],
            timeout=15,
        )
        if code == TIMED_OUT:
            raise WorktreeError("GIT_TIMEOUT", f"git did not list branches: {err}")
        taken = {line.strip() for line in out.splitlines() if line.strip()}
        candidates = [f"{BRANCH_PREFIX}{slug}"]
        for n in range(2, 100):
            candidates.append(f"{BRANCH_PREFIX}{slug}-{n}")
        for candidate in candidates:
            if candidate not in taken:
                return candidate
        raise WorktreeError("WORKTREE_FAILED", f"cannot allocate a branch for {slug!r}")


__all__ = [
    "BRANCH_PREFIX",
    "WORKTREE_PARENT",
    "WorktreeError",
    "WorktreeInfo",
    "WorktreeManager",
]
