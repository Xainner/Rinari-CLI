"""Query, review, and conflict-safe undo for persisted TurnChangeSets."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from rinari.changes.blobs import ChangeBlobStore
from rinari.changes.tracker import capture, public_file
from rinari.shared.clock import now_iso
from rinari.shared.errors import InvalidUsageError


class TurnChangeService:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.blobs = ChangeBlobStore(ctx.layout.root)

    def begin(self, services, record, turn_id: str, worktree=None):
        from rinari.changes.tracker import TurnChangeTracker

        return TurnChangeTracker(services, record, turn_id, worktree=worktree)

    def get(self, turn_id: str) -> dict[str, Any]:
        row = self.ctx.turn_change_repo.get_by_turn(turn_id)
        if row is None:
            raise InvalidUsageError(f"Turn ChangeSet not found: {turn_id}")
        return self._public(row)

    def review(self, turn_id: str, path: str | None = None) -> dict[str, Any]:
        changeset = self.get(turn_id)
        files = changeset["files"]
        if path is not None:
            files = [row for row in files if row["path"] == path or row["absolute_path"] == path]
            if not files:
                raise InvalidUsageError(f"Changed file not found in turn: {path}")
        return {"changeset_id": changeset["id"], "turn_id": turn_id, "files": files}

    def preview(self, turn_id: str, paths: list[str] | None = None) -> dict[str, Any]:
        changeset = self.ctx.turn_change_repo.get_by_turn(turn_id)
        if changeset is None:
            raise InvalidUsageError(f"Turn ChangeSet not found: {turn_id}")
        selected = self._selected(changeset["files"], paths)
        operations: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for row in selected:
            operation = self._operation(row)
            (operations if operation["safe"] else conflicts).append(operation)
        return {
            "changeset_id": changeset["id"],
            "turn_id": turn_id,
            "operations": operations,
            "conflicts": conflicts,
        }

    def undo(
        self, turn_id: str, paths: list[str] | None = None, *, apply_safe_only: bool = False
    ) -> dict[str, Any]:
        changeset = self.ctx.turn_change_repo.get_by_turn(turn_id)
        if changeset is None:
            raise InvalidUsageError(f"Turn ChangeSet not found: {turn_id}")
        preview = self.preview(turn_id, paths)
        if preview["conflicts"] and not apply_safe_only:
            return self._audit(
                changeset,
                preview,
                status="conflicted",
                applied=[],
                skipped=[item["path"] for item in preview["conflicts"]],
            )
        operation_id = self.ctx.ids.new("undo")
        applied: list[str] = []
        skipped = [item["path"] for item in preview["conflicts"]]
        by_path = {row["absolute_path"]: row for row in changeset["files"]}
        for operation in preview["operations"]:
            row = by_path[operation["absolute_path"]]
            revalidated = self._operation(row)
            if not revalidated["safe"]:
                preview["conflicts"].append(revalidated)
                skipped.append(row["path"])
                continue
            target = Path(row["absolute_path"])
            try:
                if operation["action"] == "delete":
                    target.unlink()
                elif operation["action"] == "restore":
                    content = self.blobs.read(row["before_blob_ref"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(f".{target.name}.{operation_id}.tmp")
                    temporary.write_bytes(content)
                    os.replace(temporary, target)
                elif operation["action"] == "rename_back":
                    previous = Path(row["previous_path"])
                    previous.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, previous)
            except OSError as exc:
                preview["conflicts"].append(
                    {**operation, "safe": False, "reason": type(exc).__name__}
                )
                skipped.append(row["path"])
                continue
            applied.append(row["path"])
        status = (
            "partially_undone" if skipped and applied else "conflicted" if skipped else "undone"
        )
        return self._audit(
            changeset,
            preview,
            status=status,
            applied=applied,
            skipped=skipped,
            operation_id=operation_id,
        )

    def _audit(
        self,
        changeset: dict[str, Any],
        preview: dict[str, Any],
        *,
        status: str,
        applied: list[str],
        skipped: list[str],
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        operation_id = operation_id or self.ctx.ids.new("undo")
        self.ctx.turn_change_repo.set_status(changeset["id"], status)
        completed = now_iso(self.ctx.clock)
        self.ctx.turn_change_repo.insert_undo(
            {
                "id": operation_id,
                "changeset_id": changeset["id"],
                "created_at": completed,
                "completed_at": completed,
                "status": status,
                "applied": applied,
                "conflicts": preview["conflicts"],
                "skipped": skipped,
            }
        )
        return {
            **preview,
            "undo_operation_id": operation_id,
            "status": status,
            "applied": applied,
            "skipped": skipped,
        }

    def _operation(self, row: dict[str, Any]) -> dict[str, Any]:
        base = {
            "path": row["path"],
            "absolute_path": row["absolute_path"],
            "action": (
                "delete"
                if row["kind"] == "created"
                else "rename_back"
                if row["kind"] == "renamed"
                else "restore"
            ),
        }
        if not row["undoable"]:
            return {**base, "safe": False, "reason": row.get("conflict_reason") or "not_undoable"}
        current = capture(Path(row["absolute_path"]))
        if row["kind"] == "deleted":
            safe = not current.exists
        elif row["kind"] == "renamed":
            previous = Path(row["previous_path"])
            safe = current.exists and current.sha256 == row["after_hash"] and not previous.exists()
        else:
            safe = current.exists and current.sha256 == row["after_hash"]
        return {**base, "safe": safe, "reason": None if safe else "changed_after_turn"}

    @staticmethod
    def _selected(files: list[dict[str, Any]], paths: list[str] | None) -> list[dict[str, Any]]:
        if not paths:
            return files
        wanted = set(paths)
        selected = [row for row in files if row["path"] in wanted or row["absolute_path"] in wanted]
        if len(selected) != len(wanted):
            raise InvalidUsageError("One or more requested paths are not part of the TurnChangeSet")
        return selected

    @staticmethod
    def _public(changeset: dict[str, Any]) -> dict[str, Any]:
        return {**changeset, "files": [public_file(row) for row in changeset["files"]]}
