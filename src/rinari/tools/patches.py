"""Prevalidated multi-file patches with conflict-aware rollback, not a filesystem transaction."""

import hashlib
import time

from rinari.shared.errors import CancelledError
from rinari.tools.atomic import ContentConflict, replace_text
from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "old_string": {"type": "string", "minLength": 1},
        "new_string": {"type": "string"},
    },
    "required": ["old_string", "new_string"],
    "additionalProperties": False,
}
FILES_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "maxItems": 20,
    "items": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "expected_hash": {"type": "string"},
            "edits": {"type": "array", "items": EDIT_SCHEMA, "minItems": 1, "maxItems": 20},
        },
        "required": ["path", "edits"],
        "additionalProperties": False,
    },
}


def patch_files(files, ctx):
    from rinari.tools.native.fs import _resolve_write

    prepared = []
    seen = set()
    for row in files:
        path, error = _resolve_write(ctx, row["path"])
        if error is not None:
            return error
        if path in seen:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(
                    ToolErrorCode.INVALID_ARGUMENT, "Each file must appear once; combine its edits"
                ),
            )
        seen.add(path)
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(
                    ToolErrorCode.INVALID_ARGUMENT,
                    "Batch patches require existing UTF-8 files up to 1 MiB",
                ),
            )
        original = path.read_bytes().decode("utf-8")
        digest = hashlib.sha256(original.encode()).hexdigest()
        if row.get("expected_hash", digest) != digest:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(
                    ToolErrorCode.CONFLICT, "A file changed before patch validation"
                ),
            )
        updated = original
        for edit in row["edits"]:
            if updated.count(edit["old_string"]) != 1:
                return ToolResult(
                    ok=False,
                    error=ToolErrorInfo(
                        ToolErrorCode.CONFLICT,
                        "Each edit must match exactly once; no files were changed",
                    ),
                )
            updated = updated.replace(edit["old_string"], edit["new_string"], 1)
        if len(updated.encode("utf-8")) > 1024 * 1024:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(
                    ToolErrorCode.INVALID_ARGUMENT, "Patched output exceeds 1 MiB; no files changed"
                ),
            )
        prepared.append((path, original, updated, digest))
    committed = []
    try:
        for path, original, updated, digest in prepared:
            if ctx.cancellation:
                ctx.cancellation.throw_if_cancelled()
            if ctx.deadline_at is not None and time.time() >= ctx.deadline_at:
                raise TimeoutError("Patch deadline exceeded")
            after = replace_text(path, updated, digest)
            committed.append((path, original, after))
    except Exception as exc:
        rollback = []
        for path, original, after in reversed(committed):
            try:
                replace_text(path, original, after)
                rollback.append({"path": str(path), "restored": True})
            except (OSError, ContentConflict):
                rollback.append({"path": str(path), "restored": False})
        return ToolResult(
            ok=False,
            data={"files": rollback, "rollback_complete": all(r["restored"] for r in rollback)},
            error=ToolErrorInfo(
                (
                    ToolErrorCode.CANCELLED
                    if isinstance(exc, CancelledError)
                    else ToolErrorCode.TIMEOUT
                    if isinstance(exc, TimeoutError)
                    else ToolErrorCode.CONFLICT
                ),
                "Patch interrupted; inspect rollback status. Concurrent edits were preserved.",
            ),
        )
    return ToolResult(
        ok=True,
        data={
            "files": [{"path": str(p), "sha256": h} for p, _, h in committed],
            "file_count": len(committed),
        },
    )
