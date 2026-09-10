"""Bounded, filesystem-first attribution for one agent turn."""

from __future__ import annotations

import difflib
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rinari.policy.engine import SessionScope, classify_shell_risk, is_sensitive_file
from rinari.shared.clock import now_iso

MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_SCAN_FILES = 5000
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_TURN_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_DIFF_CHARS = 500_000
SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "__pycache__", "dist", "build", "target", ".cache"}
)


@dataclass(slots=True)
class FileState:
    path: Path
    exists: bool
    sha256: str | None = None
    size: int | None = None
    content: bytes | None = None
    binary: bool = False
    sensitive: bool = False


@dataclass(slots=True)
class ToolObservation:
    tool: str
    before: dict[str, FileState]
    roots: tuple[Path, ...]
    complete: bool = True


class TurnChangeTracker:
    def __init__(self, services, record, turn_id: str, worktree=None) -> None:
        self.services = services
        self.record = record
        self.turn_id = turn_id
        self.worktree = worktree
        self.created_at = now_iso(services.ctx.clock)
        self.mutating_tool_seen = False
        self.attribution_complete = True
        self.warnings: list[str] = []
        self.before: dict[str, FileState] = {}
        self.after: dict[str, FileState] = {}
        self.roots: set[str] = set()
        self.snapshot_bytes = 0

    def before_tool(self, tool: str, arguments: dict[str, Any], ctx) -> ToolObservation | None:
        if tool in {"fs.write", "fs.patch"}:
            self.mutating_tool_seen = True
            target = self._resolve(arguments.get("path"), ctx.cwd)
            if target is None:
                return None
            state = capture(target)
            self.before.setdefault(str(target), state)
            self.roots.add(str(target.parent))
            return ToolObservation(tool, {str(target): state}, ())
        if tool not in {"shell.exec", "process.start"}:
            return None
        command = str(arguments.get("command") or "")
        scope = SessionScope(
            kind=ctx.kind,
            root=ctx.project_root if ctx.kind == "PROJECT" else ctx.cwd,
            cwd=ctx.cwd,
            profile=ctx.profile,
            user_home=ctx.user_home,
            worktree=ctx.worktree,
            private_roots=ctx.private_roots,
        )
        risk = classify_shell_risk(command, scope)
        if not risk.local_mutation:
            return None
        self.mutating_tool_seen = True
        cwd = self._resolve(arguments.get("cwd"), ctx.cwd) or ctx.cwd.resolve()
        roots: list[Path] = []
        if ctx.project_root is not None and self._inside(cwd, ctx.project_root):
            roots.append(ctx.project_root.resolve())
        elif cwd.is_dir():
            roots.append(cwd)
        targets = [Path(value) for value in risk.external_path_targets]
        before: dict[str, FileState] = {}
        complete = True
        for root in roots:
            states, scanned_all = scan(root)
            before.update(states)
            complete = complete and scanned_all
            self.roots.add(str(root))
        for target in targets:
            before[str(target.resolve())] = capture(target)
            self.roots.add(str(target.resolve().parent))
        if ctx.profile.value == "full-access":
            complete = False
        if tool == "process.start":
            complete = False
        if not complete:
            self._mark_partial(
                "A shell or process command may have modified paths outside "
                "the bounded observation."
            )
        for key, state in before.items():
            self.before.setdefault(key, state)
        return ToolObservation(tool, before, tuple(roots), complete)

    def after_tool(self, observation: ToolObservation | None) -> None:
        if observation is None:
            return
        keys = set(observation.before)
        for root in observation.roots:
            states, scanned_all = scan(root)
            keys.update(states)
            if not scanned_all:
                self._mark_partial("Workspace scan exceeded the attribution budget.")
            for key in states:
                if key not in observation.before:
                    observation.before[key] = FileState(Path(key), False)
        for key in keys:
            current = capture(Path(key))
            prior = observation.before.get(key, FileState(Path(key), False))
            if prior.sha256 == current.sha256 and prior.exists == current.exists:
                continue
            self.before.setdefault(key, prior)
            self.after[key] = current

    def finalize(self) -> dict[str, Any] | None:
        if not self.mutating_tool_seen:
            return None
        files = [self._changed_file(key) for key in sorted(self.after)]
        files = [row for row in files if row is not None]
        files = self._coalesce_renames(files)
        additions = sum(int(row.get("additions") or 0) for row in files)
        deletions = sum(int(row.get("deletions") or 0) for row in files)
        changeset = {
            "id": self.services.ctx.ids.new("chg"),
            "turn_id": self.turn_id,
            "session_id": self.record.id,
            "project_id": self.record.project_id,
            "roots": sorted(self.roots),
            "created_at": self.created_at,
            "completed_at": now_iso(self.services.ctx.clock),
            "additions": additions,
            "deletions": deletions,
            "undoable": (
                bool(files) and self.attribution_complete and all(row["undoable"] for row in files)
            ),
            "attribution_complete": self.attribution_complete,
            "warnings": list(self.warnings),
            "status": "active",
        }
        self.services.ctx.turn_change_repo.insert(changeset, files)
        return {**changeset, "files": [public_file(row) for row in files]}

    @staticmethod
    def _coalesce_renames(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deleted = {
            row["before_hash"]: row
            for row in files
            if row["kind"] == "deleted" and row.get("before_hash") and not row["sensitive"]
        }
        consumed: set[str] = set()
        result: list[dict[str, Any]] = []
        for row in files:
            source = deleted.get(row.get("after_hash")) if row["kind"] == "created" else None
            if source is None or source["absolute_path"] in consumed:
                result.append(row)
                continue
            consumed.add(source["absolute_path"])
            row.update(
                {
                    "kind": "renamed",
                    "previous_path": source["absolute_path"],
                    "before_exists": True,
                    "before_hash": source["before_hash"],
                    "before_size": source["before_size"],
                    "before_blob_ref": source["before_blob_ref"],
                    "undoable": row["undoable"] and source["undoable"],
                    "conflict_reason": source["conflict_reason"] or row["conflict_reason"],
                }
            )
            result.append(row)
        return [row for row in result if row["absolute_path"] not in consumed]

    def _changed_file(self, key: str) -> dict[str, Any] | None:
        before = self.before[key]
        after = self.after[key]
        if before.exists == after.exists and before.sha256 == after.sha256:
            return None
        kind = "created" if not before.exists else "deleted" if not after.exists else "modified"
        binary = before.binary or after.binary
        additions: int | None = None
        deletions: int | None = None
        diff: str | None = None
        truncated = False
        if not binary and not before.sensitive and not after.sensitive:
            old = (before.content or b"").decode("utf-8", errors="replace").splitlines()
            new = (after.content or b"").decode("utf-8", errors="replace").splitlines()
            lines = list(
                difflib.unified_diff(
                    old,
                    new,
                    fromfile=str(before.path),
                    tofile=str(after.path),
                    lineterm="",
                )
            )
            additions = sum(
                1 for line in lines if line.startswith("+") and not line.startswith("+++")
            )
            deletions = sum(
                1 for line in lines if line.startswith("-") and not line.startswith("---")
            )
            rendered = "\n".join(lines)
            truncated = len(rendered) > MAX_DIFF_CHARS
            diff = rendered[:MAX_DIFF_CHARS] if rendered else ""
        classification = None
        if self.worktree is not None:
            try:
                classification = self.worktree.classify(before.path)
            except Exception:
                classification = None
        ownership = "mixed" if classification is not None else "agent"
        sensitive = before.sensitive or after.sensitive
        snapshot_ok = before.content is not None or not before.exists
        within_budget = (
            before.content is None
            or self.snapshot_bytes + len(before.content) <= MAX_TURN_SNAPSHOT_BYTES
        )
        undoable = not sensitive and snapshot_ok and within_budget and ownership == "agent"
        blob_ref = None
        if undoable and before.content is not None:
            blob_ref = self.services.changes.blobs.put(before.content)
            self.snapshot_bytes += len(before.content)
        reason = None
        if sensitive:
            reason = "sensitive_file"
        elif ownership == "mixed":
            reason = "mixed_ownership"
        elif not snapshot_ok:
            reason = "snapshot_too_large"
        elif not within_budget:
            reason = "snapshot_budget_exceeded"
        root = (
            Path(self.record.project_root_snapshot).resolve()
            if self.record.project_root_snapshot
            else None
        )
        path = self._display_path(after.path, root)
        return {
            "path": path,
            "absolute_path": str(after.path),
            "previous_path": None,
            "kind": kind,
            "additions": additions,
            "deletions": deletions,
            "before_exists": before.exists,
            "after_exists": after.exists,
            "before_hash": before.sha256,
            "after_hash": after.sha256,
            "before_size": before.size,
            "after_size": after.size,
            "ownership": ownership,
            "confidence": "exact" if not sensitive else "observed",
            "binary": binary,
            "sensitive": sensitive,
            "diff": None if sensitive else diff,
            "diff_truncated": truncated,
            "undoable": undoable,
            "conflict_reason": reason,
            "before_blob_ref": blob_ref,
        }

    def _mark_partial(self, warning: str) -> None:
        self.attribution_complete = False
        if warning not in self.warnings:
            self.warnings.append(warning)

    @staticmethod
    def _resolve(value: Any, base: Path) -> Path | None:
        if value in (None, ""):
            return base.resolve()
        if not isinstance(value, str):
            return None
        candidate = Path(value)
        return (base / candidate if not candidate.is_absolute() else candidate).resolve()

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        root = root.resolve()
        path = path.resolve()
        return path == root or root in path.parents

    @staticmethod
    def _display_path(path: Path, root: Path | None) -> str:
        if root is not None:
            try:
                return path.relative_to(root).as_posix()
            except ValueError:
                pass
        return str(path)


def capture(path: Path) -> FileState:
    resolved = path.resolve()
    sensitive = is_sensitive_file(resolved)
    if not resolved.is_file():
        return FileState(resolved, False, sensitive=sensitive)
    try:
        size = resolved.stat().st_size
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        remaining = MAX_SNAPSHOT_BYTES
        with resolved.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                    remaining -= min(remaining, len(chunk))
        prefix = b"".join(chunks)
        content = prefix if size <= MAX_SNAPSHOT_BYTES and not sensitive else None
        binary = b"\x00" in prefix[:8192]
        return FileState(resolved, True, digest.hexdigest(), size, content, binary, sensitive)
    except OSError:
        return FileState(resolved, resolved.exists(), sensitive=sensitive)


def scan(root: Path) -> tuple[dict[str, FileState], bool]:
    states: dict[str, FileState] = {}
    total = 0
    complete = True
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for name in filenames:
            if len(states) >= MAX_SCAN_FILES or total >= MAX_SCAN_BYTES:
                complete = False
                return states, complete
            state = capture(Path(dirpath) / name)
            states[str(state.path)] = state
            total += int(state.size or 0)
    return states, complete


def public_file(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "before_blob_ref"}
