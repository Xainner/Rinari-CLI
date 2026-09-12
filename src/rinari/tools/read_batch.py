"""Parallel pure filesystem reads, after ToolRuntime authorizes every path."""

import time
from concurrent.futures import ThreadPoolExecutor

from rinari.shared.errors import CancelledError
from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult

PATHS_SCHEMA = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
    "minItems": 1,
    "maxItems": 16,
    "uniqueItems": True,
}


def read_batch(paths, ctx, handler):
    def read(path):
        try:
            if ctx.cancellation:
                ctx.cancellation.throw_if_cancelled()
            if ctx.deadline_at is not None and time.time() >= ctx.deadline_at:
                raise TimeoutError("Read deadline exceeded")
            result = handler({"path": path}, ctx)
        except (OSError, ValueError, CancelledError, TimeoutError) as exc:
            code = (
                ToolErrorCode.CANCELLED
                if isinstance(exc, CancelledError)
                else ToolErrorCode.TIMEOUT
                if isinstance(exc, TimeoutError)
                else ToolErrorCode.NOT_FOUND
                if isinstance(exc, FileNotFoundError)
                else ToolErrorCode.INVALID_ARGUMENT
            )
            result = ToolResult(ok=False, error=ToolErrorInfo(code, type(exc).__name__))
        return {
            "path": path,
            "ok": result.ok,
            "data": result.data,
            "error": (
                {"code": result.error.code.value, "message": result.error.message}
                if result.error
                else None
            ),
        }

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="rinari-read") as pool:
        rows = list(pool.map(read, paths))
    success = all(row["ok"] for row in rows)
    return ToolResult(
        ok=success,
        data={"files": rows, "file_count": len(rows)},
        error=None
        if success
        else ToolErrorInfo(
            ToolErrorCode(next(row["error"]["code"] for row in rows if row["error"])),
            "Some reads failed; inspect each file result",
        ),
    )
