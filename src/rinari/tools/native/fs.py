"""Native filesystem tools (tools.md section 2, phase 2 subset).

Every path is canonicalized through the sandbox; the policy engine has
already decided the capability, the sandbox enforces the granted roots.
Binary detection, truncation, and artifact spill keep model context
bounded (harness.md section 65).
"""

from __future__ import annotations

import fnmatch
import os
import time
from pathlib import Path
from typing import Any

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
    resolved, error = _resolve_read(ctx, input.get("path"))
    if error:
        return error
    if resolved.is_dir():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "Path is a directory; use fs.list")
    raw = read_text_bounded(resolved)
    return _ok(
        {
            "path": str(resolved),
            "size_bytes": resolved.stat().st_size,
            "text": raw.text,
            "truncated": raw.truncated,
        }
    )


def read_text_bounded(path: Path, max_bytes: int = MAX_READ_BYTES) -> _Bounded:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return _Bounded(text=f"[unreadable: {exc.__class__.__name__}]", truncated=False)
    truncated = len(raw) > max_bytes
    data = raw[:max_bytes]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _Bounded(text="[binary file - content not shown]", truncated=truncated)
    return _Bounded(text=text, truncated=truncated)


class _Bounded:
    def __init__(self, text: str, truncated: bool) -> None:
        self.text = text
        self.truncated = truncated


# -- fs.read_lines ------------------------------------------------------------


def fs_read_lines(input: dict, ctx: ToolContext) -> ToolResult:
    resolved, error = _resolve_read(ctx, input.get("path"))
    if error:
        return error
    raw = read_text_bounded(resolved, max_bytes=1_000_000)
    lines = raw.text.splitlines()
    start = input.get("start", 1)
    end = input.get("end", len(lines))
    try:
        start, end = int(start), int(end)
    except (TypeError, ValueError):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "start/end must be integers")
    start = max(1, start)
    end = min(len(lines), max(start, end))
    selected = [
        {"line": start + i, "text": content} for i, content in enumerate(lines[start - 1 : end])
    ]
    return _ok({"path": str(resolved), "lines": selected, "total_lines": len(lines)})


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
        resolved.write_text(content, encoding="utf-8")
    except OSError as exc:
        return _fail(ToolErrorCode.PERMISSION_DENIED, f"Write failed: {exc.__class__.__name__}")
    data: dict = {"path": str(resolved), "bytes_written": len(content.encode("utf-8"))}
    if pre_existing is not None:
        data["user_pre_existing_changes"] = True
        data["warning"] = (
            f"this file had uncommitted user changes from before the session ({pre_existing})"
        )
    return _ok(data)


# -- fs.patch -----------------------------------------------------------------


def fs_patch(input: dict, ctx: ToolContext) -> ToolResult:
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
        original = resolved.read_text(encoding="utf-8")
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
        resolved.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return _fail(ToolErrorCode.PERMISSION_DENIED, f"Write failed: {exc.__class__.__name__}")
    data = {
        "path": str(resolved),
        "replacements": count if input.get("replace_all", False) else 1,
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
        for entry in sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if len(entries) >= MAX_LIST_ENTRIES:
                break
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
    return _ok({"path": str(resolved), "entries": entries})


# -- fs.glob --------------------------------------------------------------------


def fs_glob(input: dict, ctx: ToolContext) -> ToolResult:
    base_arg = input.get("path") or "."
    resolved, error = _resolve_read(ctx, base_arg)
    if error:
        return error
    pattern = input.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "pattern must be a non-empty string")
    matches: list[str] = []
    try:
        for path in resolved.glob(pattern):
            if len(matches) >= 500:
                break
            try:
                ctx.sandbox.assert_readable(path.resolve())
            except Exception:
                continue
            matches.append(str(path))
    except OSError as exc:
        return _fail(ToolErrorCode.PERMISSION_DENIED, f"Glob failed: {exc.__class__.__name__}")
    return _ok({"root": str(resolved), "pattern": pattern, "matches": sorted(matches)})


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
    include = input.get("include")
    needle = pattern.lower()
    files = [resolved] if resolved.is_file() else list(_walk_text_files(resolved, include))
    matches = []
    searched = 0
    for file in files:
        if not _is_text_file(file):
            continue
        searched += 1
        try:
            lines = file.read_text(encoding="utf-8", errors="strict").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        file_hits = [
            {"line": i + 1, "text": line.rstrip()[:500]}
            for i, line in enumerate(lines)
            if needle in line.lower()
        ]
        if file_hits:
            matches.append({"file": str(file), "lines": file_hits[:50]})
        if len(matches) >= MAX_SEARCH_RESULTS:
            break
    return _ok(
        {
            "root": str(resolved),
            "pattern": pattern,
            "files_searched": searched,
            "matches": matches,
            "truncated": len(matches) >= MAX_SEARCH_RESULTS,
        }
    )


# -- fs.stat --------------------------------------------------------------------


def fs_stat(input: dict, ctx: ToolContext) -> ToolResult:
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
    text_a = read_text_bounded(path_a, max_bytes=1_000_000).text
    text_b = read_text_bounded(path_b, max_bytes=1_000_000).text
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
    return [
        ToolDefinition(
            name="fs.read",
            description="Read a text file inside the session root.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
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
            description="Replace the exact unique old_string with new_string in a file.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=False,
            handler=fs_patch,
            namespace="fs",
        ),
        ToolDefinition(
            name="fs.list",
            description="List the entries of a directory.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
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
                },
                "required": ["path"],
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
