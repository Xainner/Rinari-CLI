"""Bounded, session-scoped access to artifacts created by tools."""

from __future__ import annotations

import codecs
import mimetypes
from pathlib import Path

from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_NONE,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

MAX_ARTIFACT_SLICE = 32 * 1024


def _error(code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(ok=False, error=ToolErrorInfo(code=code, message=message))


def _path_for(uri: object, ctx: ToolContext) -> tuple[Path | None, ToolResult | None]:
    if not isinstance(uri, str) or not uri.startswith("artifact://"):
        return None, _error(ToolErrorCode.INVALID_ARGUMENT, "uri must use artifact://")
    parts = uri.removeprefix("artifact://").split("/")
    if len(parts) != 3 or not all(parts):
        return None, _error(
            ToolErrorCode.INVALID_ARGUMENT,
            "expected artifact://<session>/<namespace>/<name>",
        )
    session_id, namespace, name = parts
    if session_id != ctx.session_id:
        return None, _error(
            ToolErrorCode.PERMISSION_DENIED,
            "artifacts are readable only from the current session",
        )
    if any(part in (".", "..") or "\\" in part for part in parts):
        return None, _error(ToolErrorCode.INVALID_ARGUMENT, "invalid artifact URI")
    root = ctx.artifact_root.resolve()
    path = (root / session_id / namespace / name).resolve()
    if root not in path.parents:
        return None, _error(ToolErrorCode.PERMISSION_DENIED, "artifact path escaped its root")
    if not path.is_file():
        return None, _error(ToolErrorCode.NOT_FOUND, f"artifact not found: {uri}")
    return path, None


def artifact_read(input: dict, ctx: ToolContext) -> ToolResult:
    path, error = _path_for(input.get("uri"), ctx)
    if error is not None:
        return error
    try:
        start = max(0, int(input.get("start_byte", 0)))
        maximum = min(MAX_ARTIFACT_SLICE, max(1, int(input.get("max_bytes", 8192))))
    except (TypeError, ValueError):
        return _error(ToolErrorCode.INVALID_ARGUMENT, "start_byte/max_bytes must be integers")
    try:
        size = path.stat().st_size
        start = min(start, size)
        with path.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read(maximum)
            decoder = codecs.getincrementaldecoder("utf-8")()
            text = decoder.decode(chunk, final=start + len(chunk) >= size)
            # Complete the last code point, at most three additional bytes.
            # A tiny requested slice must still advance without corrupting UTF-8.
            while decoder.getstate()[0]:
                extra = handle.read(1)
                chunk += extra
                text += decoder.decode(extra, final=not extra)
        if "\0" in text:
            return _error(
                ToolErrorCode.INVALID_ARGUMENT, "Artifact is binary; use export to open it."
            )
    except UnicodeDecodeError:
        return _error(
            ToolErrorCode.INVALID_ARGUMENT,
            "Artifact is not UTF-8 text or start_byte is inside a character; "
            "use a returned cursor.",
        )
    except OSError as exc:
        return _error(
            ToolErrorCode.NOT_FOUND, f"Artifact could not be read: {exc.__class__.__name__}"
        )
    end = start + len(chunk)
    return ToolResult(
        ok=True,
        data={
            "uri": input["uri"],
            "start_byte": start,
            "end_byte": end,
            "size_bytes": size,
            "truncated": end < size,
            "next_start_byte": end if end < size else None,
            "text": text,
            "encoding": "utf-8",
        },
    )


def artifact_metadata(input: dict, ctx: ToolContext) -> ToolResult:
    path, error = _path_for(input.get("uri"), ctx)
    if error is not None:
        return error
    try:
        stat = path.stat()
    except OSError:
        return _error(ToolErrorCode.NOT_FOUND, "Artifact is no longer available")
    return ToolResult(
        ok=True,
        data={
            "uri": input["uri"],
            "name": path.name,
            "size_bytes": stat.st_size,
            "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "modified_ns": stat.st_mtime_ns,
        },
    )


def artifact_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="artifact.read",
            description=(
                "Read a bounded byte slice from an artifact:// URI produced by a prior tool. "
                "Use next_start_byte to continue. UTF-8 only; a slice can extend up to "
                "three bytes to complete its last character."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uri": {"type": "string"},
                    "start_byte": {"type": "integer", "minimum": 0},
                    "max_bytes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_ARTIFACT_SLICE,
                    },
                },
                "required": ["uri"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=artifact_read,
            namespace="artifact",
        ),
        ToolDefinition(
            name="artifact.metadata",
            description="Return metadata for an artifact:// URI from the current session.",
            input_schema={
                "type": "object",
                "properties": {"uri": {"type": "string"}},
                "required": ["uri"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=artifact_metadata,
            namespace="artifact",
        ),
    ]


__all__ = ["artifact_tools"]
