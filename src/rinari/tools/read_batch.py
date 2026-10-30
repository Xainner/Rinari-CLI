"""Parallel pure filesystem reads, after ToolRuntime authorizes every path."""

import time

from rinari.shared.errors import CancelledError
from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult
from rinari.tools.executor import map_reads

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
            # Each file of the batch pages within its share of the budget.
            result = handler({"path": path, "_page_chars": shares.get(path)}, ctx)
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

    shares = _shares(paths, ctx)
    rows = map_reads(read, paths, limit=ctx.max_concurrency)
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


def _shares(paths, ctx) -> dict:
    """Max-min fair page sizes: small files whole, large ones share the rest."""

    # Room for the batch envelope (paths, hashes); the rest is text.
    budget = int(ctx.observation_budget_bytes * 0.92) - 200 * len(paths)
    sizes = {}
    for path in paths:
        try:
            sizes[path] = ctx.sandbox.resolve(path, base=ctx.cwd).stat().st_size
        except Exception:
            sizes[path] = 0
    shares = {}
    pending = sorted(paths, key=lambda item: sizes[item])
    remaining = budget
    while pending:
        share = max(2000, remaining // len(pending))
        path = pending[0]
        if sizes[path] <= share:
            shares[path] = None  # fits whole
            remaining -= sizes[path]
            pending.pop(0)
            continue
        for path in pending:
            shares[path] = share
        break
    return shares
