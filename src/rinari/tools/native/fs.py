"""Native filesystem tools (tools.md section 2, phase 2 subset).

Every path is canonicalized through the sandbox; the policy engine has
already decided the capability, the sandbox enforces the granted roots.
Binary detection, truncation, and artifact spill keep model context
bounded (harness.md section 65).
"""

from __future__ import annotations

import codecs
import fnmatch
import hashlib
import os
import time
from pathlib import Path
from typing import Any

from rinari.tools.atomic import ContentConflict, replace_text
from rinari.tools.definition import (
    RISK_LOW,
    RISK_MEDIUM,
    SIDE_EFFECT_LOCAL_REVERSIBLE,
    SIDE_EFFECT_NONE,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

MAX_READ_BYTES = 256 * 1024
MAX_LIST_ENTRIES = 500
MAX_SEARCH_RESULTS = 200


def _ok(data: Any) -> ToolResult:
    return ToolResult(ok=True, data=data)


def _fail(code: ToolErrorCode, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(
        ok=False, error=ToolErrorInfo(code=code, message=message, retryable=retryable)
    )


def _resolve_read(ctx: ToolContext, path: Any) -> tuple[Path | None, ToolResult | None]:
    if not isinstance(path, str) or not path:
        return None, _fail(ToolErrorCode.INVALID_ARGUMENT, "path must be a non-empty string")
    try:
        resolved = ctx.sandbox.resolve(path, base=ctx.cwd)
        ctx.sandbox.assert_readable(resolved)
    except Exception as exc:  # SandboxViolationError
        return None, _fail(ToolErrorCode.SANDBOX_VIOLATION, getattr(exc, "message", str(exc)))
    if not resolved.exists():
        return None, _fail(ToolErrorCode.NOT_FOUND, f"Path does not exist: {path}")
    return resolved, None


def _resolve_write(ctx: ToolContext, path: Any) -> tuple[Path | None, ToolResult | None]:
    if not isinstance(path, str) or not path:
        return None, _fail(ToolErrorCode.INVALID_ARGUMENT, "path must be a non-empty string")
    try:
        resolved = ctx.sandbox.resolve(path, base=ctx.cwd)
        ctx.sandbox.assert_writable(resolved)
    except Exception as exc:  # SandboxViolationError
        return None, _fail(ToolErrorCode.SANDBOX_VIOLATION, getattr(exc, "message", str(exc)))
    return resolved, None


def _classify_user_work(ctx: ToolContext, resolved: Path) -> str | None:
    """'user-dirty' | 'modified-in-session' | None via the session baseline."""
    guard = ctx.worktree
    if guard is None:
        return None
    try:
        return guard.classify(resolved)
    except Exception:
        return None


# -- fs.read ------------------------------------------------------------------


def fs_read(input: dict, ctx: ToolContext) -> ToolResult:
    if "paths" in input:
        from rinari.tools.read_batch import read_batch

        return read_batch(input["paths"], ctx, fs_read)
    resolved, error = _resolve_read(ctx, input.get("path"))
    if error:
        return error
    if not resolved.is_file():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "Path is a directory; use fs.list")
    raw = read_text_bounded(resolved)
    return _ok(
        {
            "path": str(resolved),
            "size_bytes": resolved.stat().st_size,
            "text": raw.text,
            "truncated": raw.truncated,
            "sha256": raw.sha256,
        }
    )


def read_text_bounded(path: Path, max_bytes: int = MAX_READ_BYTES) -> _Bounded:
    try:
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError as exc:
        return _Bounded(text=f"[unreadable: {exc.__class__.__name__}]", truncated=False)
    truncated = len(raw) > max_bytes
    data = raw[:max_bytes]
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(data, final=not truncated)
    except UnicodeDecodeError:
        return _Bounded(text="[binary file - content not shown]", truncated=truncated)
    return _Bounded(
        text=text,
        truncated=truncated,
        sha256=hashlib.sha256(data).hexdigest() if not truncated else None,
    )


class _Bounded:
    def __init__(self, text: str, truncated: bool, sha256: str | None = None) -> None:
        self.text = text
        self.truncated = truncated
        self.sha256 = sha256


# -- fs.read_lines ------------------------------------------------------------


def fs_read_lines(input: dict, ctx: ToolContext) -> ToolResult:
    resolved, error = _resolve_read(ctx, input.get("path"))
    if error:
        return error
    try:
        start = max(1, int(input.get("start", 1)))
        end = int(input.get("end", start + 199))
        if end < start:
            return _fail(ToolErrorCode.INVALID_ARGUMENT, "end must be >= start")
        end = min(end, start + 1999)
        selected = []
        number = 0
        used = 0
        complete = True
        with resolved.open("r", encoding="utf-8", newline="") as stream:
            while True:
                if ctx.cancellation:
                    ctx.cancellation.throw_if_cancelled()
                if ctx.deadline_at is not None and time.time() >= ctx.deadline_at:
                    return _fail(ToolErrorCode.TIMEOUT, "Line scan deadline exhausted")
                line = stream.readline(1_000_001)
                if not line:
                    break
                if len(line) > 1_000_000:
                    return _fail(ToolErrorCode.RESOURCE_EXHAUSTED, "Line exceeds 1 MB")
                number += 1
                if number > end:
                    complete = False
                    break
                if number >= start:
                    text = line.rstrip("\r\n")
                    if used + len(text) > 64_000:
                        complete = False
                        break
                    selected.append({"line": number, "text": text})
                    used += len(text)
        return ToolResult(
            ok=True,
            data={
                "path": str(resolved),
                "lines": selected,
                "total_lines": number if complete else None,
                "next_line": None
                if complete
                else (selected[-1]["line"] + 1 if selected else start),
            },
            truncated=not complete,
        )
    except UnicodeDecodeError:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "File is not valid UTF-8 text")
    except OSError as exc:
        return _fail(ToolErrorCode.PERMISSION_DENIED, f"Read failed: {type(exc).__name__}")
    except (TypeError, ValueError):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "start/end must be integers")


# -- fs.write -----------------------------------------------------------------


def fs_write(input: dict, ctx: ToolContext) -> ToolResult:
    resolved, error = _resolve_write(ctx, input.get("path"))
    if error:
        return error
    content = input.get("content")
    if not isinstance(content, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "content must be a string")
    create_parents = bool(input.get("create_parents", False))
    # Classified before the write: the write itself changes the content hash.
    pre_existing = _classify_user_work(ctx, resolved)
    try:
        if create_parents:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        digest = replace_text(resolved, content, input.get("expected_hash"))
    except ContentConflict as exc:
        return _fail(ToolErrorCode.CONFLICT, str(exc))
    except OSError as exc:
        return _fail(ToolErrorCode.PERMISSION_DENIED, f"Write failed: {exc.__class__.__name__}")
    data: dict = {
        "path": str(resolved),
        "bytes_written": len(content.encode("utf-8")),
        "sha256": digest,
    }
    if pre_existing is not None:
        data["user_pre_existing_changes"] = True
        data["warning"] = (
            f"this file had uncommitted user changes from before the session ({pre_existing})"
        )
    return _ok(data)


# -- fs.patch -----------------------------------------------------------------


def fs_patch(input: dict, ctx: ToolContext) -> ToolResult:
    if "files" in input:
        from rinari.tools.patches import patch_files

        return patch_files(input["files"], ctx)
    resolved, error = _resolve_write(ctx, input.get("path"))
    if error:
        return error
    old = input.get("old_string")
    new = input.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "old_string and new_string must be strings")
    if not old:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "old_string must not be empty")
    try:
        with resolved.open("r", encoding="utf-8", newline="") as stream:
            original = stream.read()
    except UnicodeDecodeError:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "Cannot patch a non-UTF-8 file")
    except OSError as exc:
        return _fail(ToolErrorCode.NOT_FOUND, f"Read failed: {exc.__class__.__name__}")
    count = original.count(old)
    if count == 0:
        return _fail(
            ToolErrorCode.NOT_FOUND,
            "old_string not found in file",
            retryable=False,
        )
    if count > 1 and not input.get("replace_all", False):
        return _fail(
            ToolErrorCode.CONFLICT,
            f"old_string matches {count} times; provide more context or set replace_all",
        )
    updated = (
        original.replace(old, new)
        if input.get("replace_all", False)
        else original.replace(old, new, 1)
    )
    pre_existing = _classify_user_work(ctx, resolved)
    try:
        digest = replace_text(
            resolved,
            updated,
            input.get("expected_hash") or hashlib.sha256(original.encode("utf-8")).hexdigest(),
        )
    except ContentConflict as exc:
        return _fail(ToolErrorCode.CONFLICT, str(exc))
    except OSError as exc:
        return _fail(ToolErrorCode.PERMISSION_DENIED, f"Write failed: {exc.__class__.__name__}")
    data = {
        "path": str(resolved),
        "replacements": count if input.get("replace_all", False) else 1,
        "sha256": digest,
    }
    if pre_existing is not None:
        data["user_pre_existing_changes"] = True
        data["warning"] = (
            f"this file had uncommitted user changes from before the session ({pre_existing})"
        )
    return _ok(data)


# -- fs.list --------------------------------------------------------------------


def fs_list(input: dict, ctx: ToolContext) -> ToolResult:
    resolved, error = _resolve_read(ctx, input.get("path", "."))
    if error:
        return error
    if not resolved.is_dir():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "Path is not a directory")
    entries = []
    try:
        offset = max(0, int(input.get("offset", 0)))
        limit = max(1, min(MAX_LIST_ENTRIES, int(input.get("limit", MAX_LIST_ENTRIES))))
        revision = str(resolved.stat().st_mtime_ns)
        if input.get("revision") is not None and input["revision"] != revision:
            return _fail(ToolErrorCode.CONFLICT, "Directory changed; restart pagination")
        all_entries = sorted(
            resolved.iterdir(), key=lambda p: (p.is_file(), p.name.lower(), p.name)
        )
        for entry in all_entries[offset : offset + limit]:
            try:
                size = entry.stat().st_size if entry.is_file() else None
            except OSError:
                size = None
            entries.append(
                {
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "size_bytes": size,
                }
            )
    except OSError as exc:
        return _fail(ToolErrorCode.PERMISSION_DENIED, f"List failed: {exc.__class__.__name__}")
    except (TypeError, ValueError):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "offset/limit must be integers")
    return _ok(
        {
            "path": str(resolved),
            "entries": entries,
            "revision": revision,
            "total_entries": len(all_entries),
            "next_offset": offset + len(entries)
            if offset + len(entries) < len(all_entries)
            else None,
        }
    )


# -- fs.glob --------------------------------------------------------------------


def fs_glob(input: dict, ctx: ToolContext) -> ToolResult:
    base_arg = input.get("path") or "."
    resolved, error = _resolve_read(ctx, base_arg)
    if error:
        return error
    pattern = input.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "pattern must be a non-empty string")
    from rinari.tools.file_search import file_matches

    try:
        return _ok(
            file_matches(
                resolved,
                pattern,
                ctx,
                limit=min(500, max(1, int(input.get("limit", 500)))),
                offset=max(0, int(input.get("offset", 0))),
            )
        )
    except ValueError as exc:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))


def _walk_text_files(root: Path, include: str | None):
    for dirpath, dirnames, filenames in os.walk(root):
        skip = (".git", "node_modules", "__pycache__", ".venv")
        dirnames[:] = [d for d in dirnames if d not in skip]
        for filename in filenames:
            if include and not fnmatch.fnmatch(filename, include):
                continue
            yield Path(dirpath) / filename


def _is_text_file(path: Path) -> bool:
    if path.suffix.lower() in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".woff",
        ".woff2",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".pyc",
        ".so",
        ".dll",
        ".exe",
    }:
        return False
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(8192)
    except OSError:
        return False
    return b"\x00" not in chunk


# -- fs.search_text ---------------------------------------------------------------


def fs_search_text(input: dict, ctx: ToolContext) -> ToolResult:
    base_arg = input.get("path") or "."
    resolved, error = _resolve_read(ctx, base_arg)
    if error:
        return error
    pattern = input.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "pattern must be a non-empty string")
    from rinari.tools.text_search import search_text

    return search_text(resolved, input, ctx, literal=True)


# -- fs.stat --------------------------------------------------------------------


def fs_stat(input: dict, ctx: ToolContext) -> ToolResult:
    if "paths" in input:
        from rinari.tools.read_batch import read_batch

        return read_batch(input["paths"], ctx, fs_stat)
    resolved, error = _resolve_read(ctx, input.get("path"))
    if error:
        return error
    try:
        stat = resolved.stat()
    except OSError as exc:
        return _fail(ToolErrorCode.PERMISSION_DENIED, f"stat failed: {exc.__class__.__name__}")
    return _ok(
        {
            "path": str(resolved),
            "type": "dir" if resolved.is_dir() else "file",
            "size_bytes": stat.st_size,
            "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stat.st_mtime)),
            "executable": os.access(resolved, os.X_OK),
        }
    )


# -- fs.diff ----------------------------------------------------------------------


def _count_changes(diff: list[str]) -> int:
    count = 0
    for line in diff:
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            count += 1
    return count


def fs_diff(input: dict, ctx: ToolContext) -> ToolResult:
    import difflib

    path_a, error = _resolve_read(ctx, input.get("a"))
    if error:
        return error
    path_b, error = _resolve_read(ctx, input.get("b"))
    if error:
        return error
    source_a = read_text_bounded(path_a, max_bytes=1_000_000)
    source_b = read_text_bounded(path_b, max_bytes=1_000_000)
    if source_a.truncated or source_b.truncated:
        return _fail(
            ToolErrorCode.RESOURCE_EXHAUSTED,
            "Input exceeds the diff limit; compare targeted ranges or use git.diff. "
            "No complete comparison was performed.",
        )
    text_a, text_b = source_a.text, source_b.text
    diff = list(
        difflib.unified_diff(
            text_a.splitlines(),
            text_b.splitlines(),
            fromfile=str(path_a),
            tofile=str(path_b),
            lineterm="",
        )
    )
    return _ok(
        {
            "a": str(path_a),
            "b": str(path_b),
            "diff": "\n".join(diff[:2000]),
            "truncated": len(diff) > 2000,
            "changes": _count_changes(diff),
        }
    )


# -- registry -----------------------------------------------------------------------


def filesystem_tools() -> list[ToolDefinition]:
    from rinari.tools.patches import FILES_SCHEMA
    from rinari.tools.read_batch import PATHS_SCHEMA

    return [
        ToolDefinition(
            name="fs.read",
            description=(
                "Read a text file; use paths for up to 16 independent files in one "
                "parallel batch. Every path is permission checked."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "paths": PATHS_SCHEMA,
                },
                "oneOf": [{"required": ["path"]}, {"required": ["paths"]}],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=fs_read,
            namespace="fs",
        ),
        ToolDefinition(
            name="fs.read_lines",
            description="Read a 1-indexed inclusive line range from a text file.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer", "minimum": 1},
                    "end": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=fs_read_lines,
            namespace="fs",
        ),
        ToolDefinition(
            name="fs.write",
            description="Write a text file (overwrites). create_parents makes missing directories.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "create_parents": {"type": "boolean"},
                    "expected_hash": {
                        "type": "string",
                        "description": "SHA-256 of the current file, or missing for create-only.",
                    },
                },
                "required": ["path", "content"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=True,
            handler=fs_write,
            namespace="fs",
        ),
        ToolDefinition(
            name="fs.patch",
            description=(
                "Replace unique text, or supply files with edits for a prevalidated "
                "multi-file patch. Rollback never overwrites concurrent changes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                    "files": FILES_SCHEMA,
                    "expected_hash": {
                        "type": "string",
                        "description": "SHA-256 of the file previously read.",
                    },
                },
                "oneOf": [
                    {"required": ["path", "old_string", "new_string"]},
                    {"required": ["files"]},
                ],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=False,
            handler=fs_patch,
            namespace="fs",
        ),
        ToolDefinition(
            name="fs.list",
            description="List a directory page. Continue with next_offset and revision.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_ENTRIES},
                    "revision": {"type": "string"},
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=fs_list,
            namespace="fs",
        ),
        ToolDefinition(
            name="fs.glob",
            description="Find files matching a glob pattern under a root.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=fs_glob,
            namespace="fs",
        ),
        ToolDefinition(
            name="fs.search_text",
            description="Case-insensitive literal text search across text files.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "include": {"type": "string"},
                },
                "required": ["pattern"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=fs_search_text,
            namespace="fs",
        ),
        ToolDefinition(
            name="fs.stat",
            description="Filesystem metadata for a path.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "paths": PATHS_SCHEMA,
                },
                "oneOf": [{"required": ["path"]}, {"required": ["paths"]}],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=fs_stat,
            namespace="fs",
        ),
        ToolDefinition(
            name="fs.diff",
            description="Unified diff between two text files.",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "string"},
                    "b": {"type": "string"},
                },
                "required": ["a", "b"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=fs_diff,
            namespace="fs",
        ),
    ]
