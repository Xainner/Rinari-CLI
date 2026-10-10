"""Native filesystem tools (tools.md section 2, phase 2 subset).

Every path is canonicalized through the sandbox; the policy engine has
already decided the capability, the sandbox enforces the granted roots.
Binary detection, truncation, and artifact spill keep model context
bounded (harness.md section 65).
"""

from __future__ import annotations

import codecs
import dataclasses
import fnmatch
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

from rinari.tools import normalize
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
from rinari.tools.read_cache import MIN_DEDUPE_CHARS, holds_rows, numbered_rows

MAX_READ_BYTES = 256 * 1024
MAX_LIST_ENTRIES = 500
MAX_SEARCH_RESULTS = 200
FRESH_SCHEMA = {
    "type": "boolean",
    "description": "Return the full text even if an earlier read already showed it.",
}


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

        fresh = bool(input.get("fresh"))
        return read_batch(input["paths"], ctx, lambda args, c: fs_read({**args, "fresh": fresh}, c))
    resolved, error = _resolve_read(ctx, input.get("path"))
    if error:
        return error
    if not resolved.is_file():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "Path is a directory; use fs.list")
    if resolved.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return _fail(
            ToolErrorCode.INVALID_ARGUMENT,
            "This is an image. Use fs.read_image to view its pixels, not fs.read.",
        )
    raw = read_text_bounded(resolved)
    if raw.error:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, raw.error)
    size = resolved.stat().st_size
    whole = not raw.truncated and ctx.reads is not None
    if whole and not input.get("fresh") and len(raw.text) >= MIN_DEDUPE_CHARS:
        earlier = ctx.reads.earlier(
            str(resolved),
            lambda tool, data: (
                tool == "fs.read" and not data.get("truncated") and data.get("text") == raw.text
            ),
        )
        if earlier is not None:
            return _ok(
                {
                    "path": str(resolved),
                    "size_bytes": size,
                    "sha256": raw.sha256,
                    "truncated": False,
                    "unchanged": True,
                    "note": earlier.note(),
                }
            )
    if whole:
        ctx.reads.record(str(resolved), ctx.tool_call_id)
    return _ok(
        {
            "path": str(resolved),
            "size_bytes": size,
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
        return _Bounded(
            text="", truncated=False, error=f"File unreadable: {exc.__class__.__name__}"
        )
    truncated = len(raw) > max_bytes
    data = raw[:max_bytes]
    if b"\0" in data:
        return _Bounded(text="", truncated=truncated, error="File is binary or not UTF-8 text")
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(data, final=not truncated)
    except UnicodeDecodeError:
        return _Bounded(text="", truncated=truncated, error="File is binary or not UTF-8 text")
    return _Bounded(
        text=text,
        truncated=truncated,
        sha256=hashlib.sha256(data).hexdigest() if not truncated else None,
    )


class _Bounded:
    def __init__(
        self, text: str, truncated: bool, sha256: str | None = None, error: str | None = None
    ) -> None:
        self.error = error
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
        # "N| text" rows: a {"line", "text"} object per line spent about a
        # third of the observation on keys and quotes.
        selected: list[str] = []
        last = None
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
                    selected.append(f"{number}| {text}")
                    last = number
                    used += len(text)
        rows = "\n".join(selected)
        data = {
            "path": str(resolved),
            "text": rows,
            "start_line": start,
            "end_line": last,
            "total_lines": number if complete else None,
            "next_line": None if complete else (last + 1 if last is not None else start),
        }
        if ctx.reads is not None and not input.get("fresh") and len(rows) >= MIN_DEDUPE_CHARS:
            earlier = ctx.reads.earlier(
                str(resolved), lambda tool, seen: _holds_range(tool, seen, rows, start, last)
            )
            if earlier is not None:
                del data["text"]
                data.update(unchanged=True, note=earlier.note())
                return ToolResult(ok=True, data=data, truncated=not complete)
        if ctx.reads is not None and selected:
            ctx.reads.record(str(resolved), ctx.tool_call_id)
        return ToolResult(ok=True, data=data, truncated=not complete)
    except UnicodeDecodeError:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "File is not valid UTF-8 text")
    except OSError as exc:
        return _fail(ToolErrorCode.PERMISSION_DENIED, f"Read failed: {type(exc).__name__}")
    except (TypeError, ValueError):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "start/end must be integers")


def _holds_range(tool: str, seen: dict, rows: str, start: int, last: int | None) -> bool:
    """Whether an earlier read already showed exactly these rows of the file."""
    text = seen.get("text")
    if last is None or not isinstance(text, str):
        return False
    if tool == "fs.read_lines":
        return holds_rows(text, rows)
    if tool == "fs.read" and not seen.get("truncated"):
        return numbered_rows(text, start, last) == rows
    return False


# -- fs.write -----------------------------------------------------------------


def _missing_parents(path: Path) -> list[Path]:
    """Ancestors of ``path`` that do not exist yet, outermost first."""
    missing: list[Path] = []
    folder = path.parent
    while not folder.exists() and folder != folder.parent:
        missing.append(folder)
        folder = folder.parent
    return list(reversed(missing))


def _inside_write_roots(ctx: ToolContext, folder: Path) -> bool:
    """Whether ``folder`` lies strictly inside a granted write root.

    Missing folders there are created without asking: writing the file was
    already authorized and the folders stay inside the same root (the
    result lists them, so a mistyped path is visible). A root granted by a
    one-off approval is the target's own folder, never strictly inside, so
    approved writes outside the workspace still require create_parents.

    Full access grants no explicit roots (it may write anywhere), so the
    session's own working folders count as its roots: otherwise the most
    permissive profile would be the only one that cannot create `src/` in
    its own project.
    """
    roots: tuple[Path, ...] = tuple(ctx.sandbox.write_roots)
    if ctx.sandbox.unrestricted:
        from rinari.policy.sandbox import working_roots

        roots += working_roots(ctx.project_root, ctx.cwd, ctx.extra_project_roots, ctx.user_home)
    return any(root in folder.parents for root in roots)


def fs_write(input: dict, ctx: ToolContext) -> ToolResult:
    resolved, error = _resolve_write(ctx, input.get("path"))
    if error:
        return error
    content = input.get("content")
    if not isinstance(content, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "content must be a string")
    from rinari.shared.redaction import REDACTED, REDACTION_WRITE_ERROR, adds_redaction_marker

    if REDACTED in content:
        try:
            before = resolved.read_text(encoding="utf-8") if resolved.is_file() else ""
        except (OSError, UnicodeDecodeError):
            before = ""
        if adds_redaction_marker(before, content):
            return _fail(ToolErrorCode.INVALID_ARGUMENT, REDACTION_WRITE_ERROR)
    if resolved.is_dir():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, f"Path is a directory: {resolved}")
    missing = _missing_parents(resolved)
    if missing and not (
        bool(input.get("create_parents", False)) or _inside_write_roots(ctx, missing[0])
    ):
        return _fail(
            ToolErrorCode.NOT_FOUND,
            f"Parent folder does not exist: {missing[0]}. Nothing was written. Check the "
            "path; if the folder is meant to be new, retry with create_parents=true.",
        )
    # Classified before the write: the write itself changes the content hash.
    pre_existing = _classify_user_work(ctx, resolved)
    try:
        if missing:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        digest = replace_text(resolved, content, input.get("expected_hash"))
    except ContentConflict as exc:
        return _fail(ToolErrorCode.CONFLICT, str(exc))
    except FileNotFoundError:
        return _fail(
            ToolErrorCode.NOT_FOUND,
            f"Write failed: the parent folder of {resolved} does not exist",
        )
    except OSError as exc:
        return _fail(ToolErrorCode.PERMISSION_DENIED, f"Write failed: {exc.__class__.__name__}")
    data: dict = {
        "path": str(resolved),
        "bytes_written": len(content.encode("utf-8")),
        "sha256": digest,
    }
    if missing:
        data["created_dirs"] = [str(folder) for folder in missing]
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
    from rinari.shared.redaction import REDACTION_WRITE_ERROR, adds_redaction_marker

    if adds_redaction_marker(original, updated):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, REDACTION_WRITE_ERROR)
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
        # The revision only guards a continuation. On the first page there is
        # nothing to restart, and models with strict schemas send "" (or a
        # guess) for every optional field: rejecting that failed the very
        # first listing of a turn.
        expected = input.get("revision")
        if offset > 0 and expected and expected != revision:
            return _fail(ToolErrorCode.CONFLICT, "Directory changed; restart pagination")
        all_entries = sorted(
            resolved.iterdir(), key=lambda p: (p.is_file(), p.name.lower(), p.name)
        )
        for entry in all_entries[offset : offset + limit]:
            if ctx.cancellation:
                ctx.cancellation.throw_if_cancelled()
            if ctx.deadline_at is not None and time.time() >= ctx.deadline_at:
                return _fail(ToolErrorCode.TIMEOUT, "Directory scan deadline exhausted")
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
                include_dirs=bool(input.get("include_dirs", False)),
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

    regex = bool(input.get("regex", False))
    # Case-insensitive in both modes, with absolute file paths: the regex
    # switch changes how the pattern is read, not the shape of the result.
    result = search_text(resolved, input, ctx, literal=not regex, ignore_case=True, relative=False)
    if (
        result.ok
        and not regex
        and isinstance(result.data, dict)
        and not result.data.get("matches")
        and looks_like_regex(pattern)
    ):
        # Models pass `a|b` or `foo\.bar` here; a silent zero sent them on
        # to conclude the text did not exist.
        result = dataclasses.replace(
            result,
            data={
                **result.data,
                "note": "no literal match; the pattern looks like a regex — set "
                "regex=true or use search.regex",
            },
        )
    return result


_REGEX_HINTS = re.compile(
    r"\\[.\\dDwWsSbB()\[\]{}|*+?^$]"  # escaped metacharacter or class
    r"|\|"  # alternation
    r"|\.[*+?]"  # .* .+ .?
    r"|\[[^\]]+\]"  # character class
    r"|\(\?"  # group extension
    r"|^\^|\$$"  # anchors
    r"|\{\d+(,\d*)?\}"  # counted repetition
)


def looks_like_regex(pattern: str) -> bool:
    """Whether a literal search pattern was probably meant as a regex."""
    return bool(_REGEX_HINTS.search(pattern))


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
    if source_a.error or source_b.error:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, source_a.error or source_b.error)
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
            concurrency="local-read",
            description=(
                "Read a whole text file; use paths for up to 16 independent files in one "
                "parallel batch. Every path is permission checked. For a line range use "
                "fs.read_lines. Re-reading an unchanged file whose text you still have "
                "returns unchanged=true instead of the text; fresh=true forces the text."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "paths": PATHS_SCHEMA,
                    "fresh": FRESH_SCHEMA,
                },
                "oneOf": [{"required": ["path"]}, {"required": ["paths"]}],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=fs_read,
            normalize=normalize.fs_read,
            namespace="fs",
        ),
        ToolDefinition(
            name="fs.read_lines",
            concurrency="local-read",
            description=(
                "Read a 1-indexed inclusive line range from a text file (default 200 "
                "lines). text holds one 'N| line' row per line; the 'N| ' prefix is not "
                "part of the file, so drop it before quoting a line in fs.patch. Continue "
                "from next_line when it is set. Lines you already have unchanged come back "
                "as unchanged=true; fresh=true forces the text."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer", "minimum": 1},
                    "end": {"type": "integer", "minimum": 1},
                    "fresh": FRESH_SCHEMA,
                },
                "required": ["path"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=fs_read_lines,
            normalize=normalize.fs_read_lines,
            namespace="fs",
        ),
        ToolDefinition(
            name="fs.write",
            description=(
                "Write a text file (overwrites). Missing folders inside the workspace are "
                "created and listed in created_dirs; elsewhere set create_parents=true."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "create_parents": {
                        "type": "boolean",
                        "description": "Create missing folders even outside the workspace.",
                    },
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
                "Replace unique text (path, old_string, new_string), or supply "
                "files=[{path, edits:[{old_string, new_string}]}] for a prevalidated "
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
            normalize=normalize.fs_patch,
            namespace="fs",
        ),
        ToolDefinition(
            name="fs.list",
            concurrency="local-read",
            description="List a directory page. Continue with next_offset and revision.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_ENTRIES},
                    "revision": {
                        "type": "string",
                        "description": "Only when continuing (offset > 0): the revision "
                        "returned by the previous page.",
                    },
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=fs_list,
            namespace="fs",
        ),
        ToolDefinition(
            name="fs.glob",
            concurrency="local-read",
            description=(
                "Find files matching a glob pattern under a root (e.g. **/*.py). "
                "Folders match only with include_dirs=true; they end with a path separator."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                    "offset": {"type": "integer", "minimum": 0},
                    "include_dirs": {
                        "type": "boolean",
                        "description": "Also match folders (default false: files only).",
                    },
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
            concurrency="local-read",
            description=(
                "Case-insensitive text search across text files. The pattern is literal "
                "text ('a|b' searches for that exact text) unless regex=true, which reads it "
                "as a regular expression. search.regex is the case-sensitive regex search."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "include": {"type": "string"},
                    "regex": {
                        "type": "boolean",
                        "description": "Read pattern as a regular expression (default false).",
                    },
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
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
            concurrency="local-read",
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
