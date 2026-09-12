"""Context management tools (phase 4): retrieval, pins.

`context.retrieve` scores and deduplicates model-visible candidates
(repository files/symbols from the index, durable memory, session
artifacts); pinned items always rank first. `context.pin` keeps an item
model-visible every turn via the pinned-context prompt segment.
"""

from __future__ import annotations

import json
from pathlib import Path

from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_LOCAL_REVERSIBLE,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

_PIN_SOURCES = ["file", "symbol", "memory", "artifact", "term"]


def _ok(data) -> ToolResult:
    return ToolResult(ok=True, data=data)


def _fail(code: ToolErrorCode, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(
        ok=False, error=ToolErrorInfo(code=code, message=message, retryable=retryable)
    )


def _service(ctx: ToolContext):
    return getattr(ctx, "context_retrieval", None)


def _project_root(ctx: ToolContext) -> Path | None:
    root = ctx.project_root if ctx.project_root is not None else ctx.cwd
    if root is None or not Path(root).is_dir():
        return None
    return Path(root).resolve()


def _classify_write(input: dict) -> ClassifiedAction:
    return ClassifiedAction("state.write", None)


def _classify_read(input: dict) -> ClassifiedAction:
    return ClassifiedAction("state.read", None)


def context_retrieve(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    if service is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "context service unavailable")
    query = input.get("query", "")
    if not isinstance(query, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "query must be a string")
    limit = input.get("limit", 10)
    if not isinstance(limit, int) or not 1 <= limit <= 50:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "limit must be an integer 1..50")
    root = _project_root(ctx)
    try:
        results = service.retrieve(ctx.session_id, query, str(root) if root else None, limit=limit)
    except Exception as exc:
        return _fail(ToolErrorCode.UNKNOWN, getattr(exc, "message", str(exc)))
    budget = max(128, min(16000, int(input.get("max_tokens", 4000))))
    kept = []
    used = 0
    for row in results:
        cost = (len(json.dumps(row, ensure_ascii=False)) + 3) // 4
        if used + cost > budget:
            continue
        kept.append(row)
        used += cost
    return _ok(
        {
            "query": query,
            "count": len(kept),
            "results": kept,
            "tokens_estimate": used,
            "truncated": len(kept) < len(results),
        }
    )


def context_pin(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    if service is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "context service unavailable")
    source = input.get("source")
    ref = input.get("ref")
    if source not in _PIN_SOURCES:
        return _fail(
            ToolErrorCode.INVALID_ARGUMENT, f"source must be one of: {', '.join(_PIN_SOURCES)}"
        )
    if not isinstance(ref, str) or not ref.strip():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "ref is required")
    if source == "file":
        from rinari.tools.native.fs import _resolve_read

        path, error = _resolve_read(ctx, ref)
        if error is not None:
            return error
        if not path.is_file():
            return _fail(ToolErrorCode.NOT_FOUND, "Cannot pin a missing file")
        ref = str(path)
    label = input.get("label", "")
    if not isinstance(label, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "label must be a string")
    try:
        row = service.pin(ctx.session_id, source, ref.strip(), label)
    except Exception as exc:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, getattr(exc, "message", str(exc)))
    return _ok(row)


def context_unpin(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    if service is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "context service unavailable")
    source = input.get("source")
    ref = input.get("ref")
    if source not in _PIN_SOURCES:
        return _fail(
            ToolErrorCode.INVALID_ARGUMENT, f"source must be one of: {', '.join(_PIN_SOURCES)}"
        )
    if not isinstance(ref, str) or not ref.strip():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "ref is required")
    forgotten = service.unpin(ctx.session_id, source, ref.strip())
    if not forgotten and source == "file":
        forgotten = service.unpin(
            ctx.session_id, source, str(ctx.sandbox.resolve(ref, base=ctx.cwd))
        )
    return _ok({"source": source, "ref": ref.strip(), "unpinned": forgotten})


def context_list_pins(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    if service is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "context service unavailable")
    pins = service.list_pins(ctx.session_id)
    offset = max(0, int(input.get("offset", 0)))
    limit = min(100, max(1, int(input.get("limit", 50))))
    rows = pins[offset : offset + limit]
    for row in rows:
        if row.get("source") == "file":
            path = ctx.sandbox.resolve(row["ref"], base=ctx.cwd)
            row["exists"] = path.is_file()
    return _ok(
        {
            "count": len(pins),
            "pins": rows,
            "next_offset": offset + len(rows) if offset + len(rows) < len(pins) else None,
        }
    )


def context_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="context.retrieve",
            description=(
                "Retrieve relevant model-visible context for a query: repository "
                "files and symbols (project index), user/project memory, and this "
                "session's artifacts. Results are ranked (pinned first, then "
                "score), deduplicated by (source, ref), and capped by limit."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "max_tokens": {"type": "integer", "minimum": 128, "maximum": 16000},
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            timeout_ms=15_000,
            handler=context_retrieve,
            classify=_classify_read,
            namespace="context",
        ),
        ToolDefinition(
            name="context.pin",
            description=(
                "Pin a context item so it stays model-visible every turn via the "
                "pinned-context segment. source=file (repo-relative path), "
                "source=symbol (name or qualified name), source=memory (id), "
                "source=artifact (artifact:// URI), source=term (a query: the top "
                "retrieval hits for it are pinned)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": _PIN_SOURCES},
                    "ref": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["source", "ref"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=True,
            timeout_ms=15_000,
            handler=context_pin,
            classify=_classify_write,
            namespace="context",
        ),
        ToolDefinition(
            name="context.unpin",
            description="Unpin a previously pinned context item (source + ref).",
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": _PIN_SOURCES},
                    "ref": {"type": "string"},
                },
                "required": ["source", "ref"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=True,
            timeout_ms=15_000,
            handler=context_unpin,
            classify=_classify_write,
            namespace="context",
        ),
        ToolDefinition(
            name="context.list_pins",
            description="List the pinned context items for this session.",
            input_schema={
                "type": "object",
                "properties": {
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            timeout_ms=15_000,
            handler=context_list_pins,
            classify=_classify_read,
            namespace="context",
        ),
    ]
