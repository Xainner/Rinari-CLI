"""Exact argument equivalences for native tools.

Models from every provider spell the same call in a few predictable ways
(`timeout_ms` for `timeout_s`, a single-file patch with top-level `edits`).
Rejecting each variant cost a round trip; accepting it silently taught the
model nothing. A normalizer rewrites only what has exactly one meaning,
returns a note per rewrite for the model, and leaves anything ambiguous
(both spellings present, a non-numeric value) for validation to reject. It
never fills in a path, a target or a missing value.

ToolRuntime applies these before validation and classification, so policy
and approvals always judge the canonical call.
"""

from __future__ import annotations

import json
from typing import Any


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _seconds(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def _timeout_seconds(arguments: dict, notes: list[str]) -> dict:
    """`timeout_ms` (milliseconds) or `timeout` (seconds) -> `timeout_s`."""
    variants = [key for key in ("timeout_ms", "timeout") if key in arguments]
    # With the canonical key, or two variants, there is no single meaning.
    if "timeout_s" in arguments or len(variants) != 1:
        return arguments
    key = variants[0]
    value = arguments[key]
    if not _number(value):
        return arguments
    seconds = _seconds(value / 1000 if key == "timeout_ms" else value)
    rewritten = {k: v for k, v in arguments.items() if k != key}
    rewritten["timeout_s"] = seconds
    notes.append(f"used timeout_s={seconds} (received {key}={value})")
    return rewritten


def _argv_over_command(arguments: dict, notes: list[str]) -> dict:
    """Both `command` and `argv`: run argv, the literal form, and say so.

    The policy classifies argv too (it is what command_text prefers), so the
    decision covers exactly what runs.
    """
    argv = arguments.get("argv")
    if (
        "command" not in arguments
        or not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) for item in argv)
    ):
        return arguments
    notes.append("ran argv and ignored command (received both; send only one)")
    return {k: v for k, v in arguments.items() if k != "command"}


def shell_exec(arguments: dict) -> tuple[dict, list[str]]:
    notes: list[str] = []
    arguments = _timeout_seconds(arguments, notes)
    arguments = _argv_over_command(arguments, notes)
    return arguments, notes


def process_start(arguments: dict) -> tuple[dict, list[str]]:
    notes: list[str] = []
    return _argv_over_command(arguments, notes), notes


def process_wait(arguments: dict) -> tuple[dict, list[str]]:
    notes: list[str] = []
    return _timeout_seconds(arguments, notes), notes


_PATCH_SINGLE_FILE = frozenset({"path", "edits", "expected_hash"})


def fs_patch(arguments: dict) -> tuple[dict, list[str]]:
    """`{path, edits}` is the one-file form of `{files: [{path, edits}]}`.

    Only that exact shape maps: with `old_string`, `replace_all` or `files`
    alongside, the call means something else and validation rejects it.
    """
    keys = set(arguments)
    if (
        not {"path", "edits"} <= keys
        or not keys <= _PATCH_SINGLE_FILE
        or not isinstance(arguments["edits"], list)
    ):
        return arguments, []
    row = {"path": arguments["path"], "edits": arguments["edits"]}
    if "expected_hash" in arguments:
        row["expected_hash"] = arguments["expected_hash"]
    return {"files": [row]}, ["used files=[{path, edits}] (received top-level path and edits)"]


def fs_read_lines(arguments: dict) -> tuple[dict, list[str]]:
    """`start_line`/`end_line` are the same 1-indexed bounds as `start`/`end`."""
    notes: list[str] = []
    rewritten = dict(arguments)
    for variant, canonical in (("start_line", "start"), ("end_line", "end")):
        if variant in rewritten and canonical not in rewritten:
            rewritten[canonical] = rewritten.pop(variant)
            notes.append(f"used {canonical}={rewritten[canonical]} (received {variant})")
    return rewritten, notes


_LINE_RANGE_KEYS = ("start", "end", "start_line", "end_line", "offset", "limit")


def fs_read(arguments: dict) -> tuple[dict, list[str]]:
    """fs.read returns whole files; a line range belongs to fs.read_lines.

    Reading the whole file instead would not be the call the model made, so
    the range is refused with the exact call that does it.
    """
    given = [key for key in _LINE_RANGE_KEYS if key in arguments]
    if not given:
        return arguments, []
    start = next(
        (arguments[key] for key in ("start", "start_line") if _number(arguments.get(key))), 1
    )
    end = next(
        (arguments[key] for key in ("end", "end_line") if _number(arguments.get(key))),
        None,
    )
    example: dict[str, Any] = {"path": arguments.get("path") or "<file>", "start": start}
    example["end"] = end if end is not None else start + 199
    raise ValueError(
        f"fs.read reads whole files and takes no line range ({', '.join(given)}); "
        f"nothing was read. Use fs.read_lines with 1-indexed inclusive start/end, "
        f"e.g. {json.dumps(example, ensure_ascii=False)}"
    )


__all__ = [
    "fs_patch",
    "fs_read",
    "fs_read_lines",
    "process_start",
    "process_wait",
    "shell_exec",
]
