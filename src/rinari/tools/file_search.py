"""Bounded, sandbox-aware file enumeration shared by glob and search."""

from __future__ import annotations

import fnmatch
import os
import time
from collections.abc import Iterator
from pathlib import Path

from rinari.repo.search import SKIP_DIRS


def _match(parts, pattern):
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return _match(parts, pattern[1:]) or bool(parts and _match(parts[1:], pattern))
    return bool(
        parts and fnmatch.fnmatchcase(parts[0], pattern[0]) and _match(parts[1:], pattern[1:])
    )


def _walk(root: Path) -> Iterator[tuple[Path, bool]]:
    """(path, is_dir) in walk_files order, plus the folders it descends into.

    Same pruning as repo.search.walk_files (ignored and symlinked folders,
    symlinked files), so files-only results are unchanged.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in SKIP_DIRS and not d.endswith(".egg-info") and not (base / d).is_symlink()
        )
        for name in dirnames:
            yield base / name, True
        for name in sorted(filenames):
            if not (base / name).is_symlink():
                yield base / name, False


def file_matches(
    root: Path,
    pattern: str,
    ctx,
    *,
    limit: int = 500,
    offset: int = 0,
    include_dirs: bool = False,
):
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        raise ValueError("pattern must be relative without parent traversal")
    parts = Path(pattern).parts
    matches = []
    # Folders a files-only search skipped: the hint for an empty result.
    folder_matches = 0
    scanned = 0
    for path, is_dir in _walk(root):
        if not is_dir:
            scanned += 1
        if ctx.cancellation:
            ctx.cancellation.throw_if_cancelled()
        if ctx.deadline_at is not None and time.time() >= ctx.deadline_at:
            raise TimeoutError("File search deadline exhausted")
        if scanned > 20000:
            break
        if not is_dir and not path.is_file():
            continue
        relative = path.relative_to(root)
        if "**" not in parts and len(relative.parts) != len(parts):
            continue
        if not _match(relative.parts, parts):
            continue
        try:
            ctx.sandbox.assert_readable(path.resolve())
        except Exception:
            continue
        if is_dir and not include_dirs:
            folder_matches += 1
            continue
        matches.append(str(path) + os.sep if is_dir else str(path))
        if len(matches) > offset + limit:
            break
    more = len(matches) > offset + limit or scanned > 20000
    result = {
        "root": str(root),
        "pattern": pattern,
        "matches": matches[offset : offset + limit],
        "truncated": more,
        "next_offset": offset + limit if len(matches) > offset + limit else None,
        "scan_limit_reached": scanned > 20000,
        "backend": "bounded-walk",
    }
    if not matches and folder_matches:
        noun = "folder matches" if folder_matches == 1 else "folders match"
        result["note"] = (
            f"no files match; {folder_matches} {noun} the pattern. "
            "Set include_dirs=true to list folders."
        )
    return result
