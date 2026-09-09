"""Checkpoint service: create/list/restore/remove with ownership rules."""

from __future__ import annotations

from pathlib import Path

from rinari.application.context import AppContext
from rinari.checkpoints import core
from rinari.shared.errors import InvalidUsageError

CHECKPOINT_ID_PREFIX = "ckpt"


class CheckpointService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    @property
    def repo(self):
        return self._ctx.checkpoint_repo

    def _root(self, path: str | Path) -> Path:
        root = Path(path).expanduser()
        resolved = root.resolve()
        if not resolved.is_dir():
            raise InvalidUsageError(f"Not a directory: {resolved}")
        if not (resolved / ".git").exists():
            raise InvalidUsageError(
                f"Checkpoints require a Git repository: {resolved}",
                hint="Undo restores against the working tree, so a Git repo is required.",
            )
        return resolved

    def _baselines(self, session_id: str) -> dict[str, tuple[str, str | None]]:
        rows = self._ctx.worktree_repo.list(session_id)
        return {r.path: (r.git_status, r.blob_sha) for r in rows}

    def _session_for(self, root: Path, session_id: str | None) -> str:
        if session_id:
            session = self._ctx.session_repo.get(session_id)
            if session is None:
                raise InvalidUsageError(f"Session not found: {session_id}")
            return session.id
        best = None
        for session in self._ctx.session_repo.list(kind="PROJECT", limit=50):
            if (
                session.project_root_snapshot
                and Path(session.project_root_snapshot).resolve() == root
            ):
                best = session
                break
        if best is None:
            raise InvalidUsageError(
                "No project session found for this directory",
                hint="Pass --session <id>, or create the checkpoint from inside the "
                "session's repository.",
            )
        return best.id

    def create(self, path: str | Path, *, label: str = "", session_id: str | None = None) -> dict:
        root = self._root(path)
        sid = self._session_for(root, session_id)
        entries = core.classify_worktree(root, self._baselines(sid))
        agent_changes = sum(1 for e in entries if e.ownership == core.OWNERSHIP_AGENT)
        user_owned = sum(1 for e in entries if e.ownership != core.OWNERSHIP_AGENT)
        checkpoint = self.repo.insert(
            {
                "id": self._ctx.ids.new(CHECKPOINT_ID_PREFIX),
                "session_ref": sid,
                "project_root": str(root),
                "label": label.strip(),
                "agent_changes": agent_changes,
                "user_owned": user_owned,
                "created_at": _now(self._ctx.clock),
            }
        )
        self.repo.insert_files(checkpoint["id"], entries)
        return {**checkpoint, "files": [e.to_dict() for e in entries]}

    def ids_for_session(self, session_id: str) -> list[str]:
        rows = self._ctx.db.query(
            "SELECT id FROM checkpoints WHERE session_ref = ? ORDER BY created_at DESC, id DESC",
            (session_id,),
        )
        return [row["id"] for row in rows]

    def duplicate_for_session(
        self, source_id: str, target_id: str, checkpoint_id: str | None = None
    ) -> int:
        """Copy a session's checkpoints (with file rows) onto another session.

        With checkpoint_id, only checkpoints at-or-before it are copied
        (branch point in checkpoint history). Unknown or foreign ids fail
        before anything is copied.
        """
        ids = self.ids_for_session(source_id)
        if checkpoint_id is not None:
            cutoff = self.repo.get(checkpoint_id)
            if cutoff is None or cutoff.get("session_ref") != source_id:
                raise InvalidUsageError(f"Checkpoint not found in session: {checkpoint_id}")
            stamp = cutoff.get("created_at") or ""
            ids = [
                cid
                for cid in ids
                if (self.repo.get(cid) or {}).get("created_at", "") <= stamp
            ]
        copied = 0
        for cid in ids:
            row = self.repo.get(cid)
            if row is None:
                continue
            new_id = self._ctx.ids.new(CHECKPOINT_ID_PREFIX)
            self._ctx.db.execute(
                "INSERT INTO checkpoints "
                "(id, session_ref, project_root, label, agent_changes, user_owned, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    new_id,
                    target_id,
                    row["project_root"],
                    row["label"],
                    row["agent_changes"],
                    row["user_owned"],
                    row["created_at"],
                ),
            )
            self._ctx.db.execute(
                "INSERT INTO checkpoint_files "
                "(checkpoint_id, path, ownership, prior_source, prior_status, prior_blob) "
                "SELECT ?, path, ownership, prior_source, prior_status, prior_blob "
                "FROM checkpoint_files WHERE checkpoint_id = ?",
                (new_id, cid),
            )
            copied += 1
        return copied

    def list(self, path: str | Path | None = None) -> list[dict]:
        root = str(self._root(path)) if path else None
        if root is not None:
            return self.repo.list(root)
        # global list (all projects)
        rows = self._db_query_all()
        return rows

    def _db_query_all(self) -> list[dict]:
        rows = self._ctx.db.query(
            "SELECT * FROM checkpoints ORDER BY created_at DESC, id DESC LIMIT 200"
        )
        return [dict(r) for r in rows]

    def show(self, checkpoint_id: str) -> dict:
        checkpoint = self.repo.get(checkpoint_id)
        if checkpoint is None:
            raise InvalidUsageError(f"Checkpoint not found: {checkpoint_id}")
        files = self.repo.files(checkpoint_id)
        return {**checkpoint, "files": files}

    def remove(self, checkpoint_id: str) -> bool:
        return self.repo.remove(checkpoint_id)

    def resolve_target(self, path: str | Path, checkpoint_id: str | None) -> dict:
        root = self._root(path)
        checkpoint = self.repo.get(checkpoint_id) if checkpoint_id else self.repo.latest(str(root))
        if checkpoint is None:
            raise InvalidUsageError(
                "No checkpoints found",
                hint="Create one: `rinari undo create`.",
            )
        if checkpoint["project_root"] != str(root):
            raise InvalidUsageError(
                f"Checkpoint {checkpoint['id']} belongs to {checkpoint['project_root']}"
            )
        return checkpoint

    def restore(
        self,
        path: str | Path,
        *,
        checkpoint_id: str | None = None,
        preview: bool = False,
        allow_mixed: bool = False,
    ) -> dict:
        root = self._root(path)
        checkpoint = self.resolve_target(root, checkpoint_id)
        files = self.repo.files(checkpoint["id"])
        entries = [
            core.CheckpointEntry(
                path=row["path"],
                ownership=row["ownership"],
                prior_status=row["prior_status"],
                prior_sha=None,
                prior_bytes=row.get("prior_bytes"),
                exists=(row["prior_bytes"] is not None),
            )
            for row in files
        ]
        if preview:
            operations = core.plan_restore(entries, allow_mixed=allow_mixed)
            return {
                "checkpoint_id": checkpoint["id"],
                "preview": True,
                "operations": [op.to_dict() for op in operations],
            }
        operations = core.plan_restore(entries, allow_mixed=allow_mixed)
        applied: list[str] = []
        skipped: list[dict] = []
        for op, entry in zip(operations, entries, strict=True):
            if op.action == "restore":
                action = core.apply_restore(root, entry)
                if action != "noop":
                    applied.append(op.path)
            elif op.action == "skip":
                skipped.append({"path": op.path, "reason": op.reason, "ownership": op.ownership})
        return {
            "checkpoint_id": checkpoint["id"],
            "preview": False,
            "applied": applied,
            "skipped": skipped,
        }


def _now(clock) -> str:
    from rinari.shared.clock import now_iso

    return now_iso(clock)
