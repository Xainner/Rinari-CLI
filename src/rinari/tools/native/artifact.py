"""Bounded, session-scoped access to artifacts created by tools."""

from __future__ import annotations

import codecs
import hashlib
import mimetypes
import shutil
from pathlib import Path

from rinari.tools.definition import (
    RISK_LOW,
    RISK_MEDIUM,
    SIDE_EFFECT_LOCAL_REVERSIBLE,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
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
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return _error(
            ToolErrorCode.INVALID_ARGUMENT,
            "This artifact is an image. Use fs.read_image with this artifact URI in path.",
        )
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
                ToolErrorCode.INVALID_ARGUMENT,
                "Artifact is binary; use artifact.export to copy it to a file.",
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


def _plain_name(path: Path) -> str:
    """Stored name without the content-hash prefix attachments carry."""
    head, sep, rest = path.name.partition("-")
    if sep and rest and len(head) == 64 and all(c in "0123456789abcdef" for c in head):
        return rest
    return path.name


def _export_target(input: dict) -> str:
    return str(input.get("path") or "")


def _free_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for index in range(1, 1000):
        candidate = dest.with_name(f"{dest.stem} ({index}){dest.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(str(dest))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_export(input: dict, ctx: ToolContext) -> ToolResult:
    """Copy an artifact's original bytes to a real file a program can open.

    Attachments live in the artifact store, outside the workspace; an
    uploader or converter needs a path. The copy is the stored original (not
    the JPEG sent for vision), checked against its recorded hash, written
    where the write policy allows. An existing file is never overwritten.
    """
    path, error = _path_for(input.get("uri"), ctx)
    if error is not None:
        return error
    target = input.get("path") or _plain_name(path)
    if not isinstance(target, str):
        return _error(ToolErrorCode.INVALID_ARGUMENT, "path must be a string")
    try:
        dest = ctx.sandbox.resolve(target, base=ctx.cwd)
        if dest.is_dir():
            dest = dest / _plain_name(path)
        ctx.sandbox.assert_writable(dest)
    except Exception as exc:  # SandboxViolationError
        return _error(ToolErrorCode.SANDBOX_VIOLATION, getattr(exc, "message", str(exc)))
    expected = None
    store = ctx.artifact_store
    if store is not None:
        try:
            expected = store.meta(input["uri"]).sha256
        except Exception:
            expected = None
    try:
        actual = _sha256(path)
        if expected is not None and actual != expected:
            return _error(
                ToolErrorCode.CONFLICT,
                "The stored artifact no longer matches its recorded hash; nothing was copied.",
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest = _free_path(dest)
        shutil.copyfile(path, dest)
    except OSError as exc:
        return _error(ToolErrorCode.PERMISSION_DENIED, f"Export failed: {exc.__class__.__name__}")
    return ToolResult(
        ok=True,
        data={
            "uri": input["uri"],
            "path": str(dest),
            "name": dest.name,
            "size_bytes": dest.stat().st_size,
            "mime_type": mimetypes.guess_type(dest.name)[0] or "application/octet-stream",
            "sha256": actual,
        },
    )


def artifact_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="artifact.read",
            concurrency="local-read",
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
            classify=lambda _: ClassifiedAction("state.read"),
            namespace="artifact",
        ),
        ToolDefinition(
            name="artifact.metadata",
            concurrency="local-read",
            description="Return metadata for an artifact:// URI from the current session.",
            input_schema={
                "type": "object",
                "properties": {"uri": {"type": "string"}},
                "required": ["uri"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=artifact_metadata,
            classify=lambda _: ClassifiedAction("state.read"),
            namespace="artifact",
        ),
        ToolDefinition(
            name="artifact.export",
            description=(
                "Copy the original bytes of an artifact:// URI from this session (for example "
                "an attached image or document) to a real file, for programs, uploads or "
                "scripts that need a path. path is a file or folder; without it the copy goes "
                "to the working directory. Never overwrites: a taken name gets a suffix. "
                "Returns the path and sha256 of the copy."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "uri": {"type": "string"},
                    "path": {
                        "type": "string",
                        "description": "Destination file or existing folder.",
                    },
                },
                "required": ["uri"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            handler=artifact_export,
            classify=lambda a: ClassifiedAction("fs.write", _export_target(a) or "."),
            namespace="artifact",
        ),
    ]


__all__ = ["artifact_tools"]
