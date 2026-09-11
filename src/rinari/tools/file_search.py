"""Bounded, sandbox-aware file enumeration shared by glob and search."""

from __future__ import annotations

import fnmatch
import time
from pathlib import Path

from rinari.repo.search import walk_files


def _match(parts, pattern):
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return _match(parts, pattern[1:]) or bool(parts and _match(parts[1:], pattern))
    return bool(
        parts and fnmatch.fnmatchcase(parts[0], pattern[0]) and _match(parts[1:], pattern[1:])
    )


def file_matches(root: Path, pattern: str, ctx, *, limit: int = 500, offset: int = 0):
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        raise ValueError("pattern must be relative without parent traversal")
    matches = []
    scanned = 0
    for path in walk_files(root, max_files=20001):
        scanned += 1
        if ctx.cancellation:
            ctx.cancellation.throw_if_cancelled()
        if ctx.deadline_at is not None and time.time() >= ctx.deadline_at:
            raise TimeoutError("File search deadline exhausted")
        if scanned > 20000:
            break
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root)
        if "**" not in Path(pattern).parts and len(relative.parts) != len(Path(pattern).parts):
            continue
        if not _match(relative.parts, Path(pattern).parts):
            continue
        try:
            ctx.sandbox.assert_readable(path.resolve())
        except Exception:
            continue
        matches.append(str(path))
        if len(matches) > offset + limit:
            break
    more = len(matches) > offset + limit or scanned > 20000
    return {
        "root": str(root),
        "pattern": pattern,
        "matches": matches[offset : offset + limit],
        "truncated": more,
        "next_offset": offset + limit if len(matches) > offset + limit else None,
        "scan_limit_reached": scanned > 20000,
        "backend": "bounded-walk",
    }
