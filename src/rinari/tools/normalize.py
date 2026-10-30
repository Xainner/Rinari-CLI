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
    notes: list[str] = []
    if isinstance(arguments.get("edits"), str):
        # A list of edits sent as JSON text (F12): the top-level `edits` is not
        # in the schema, so the generic coercion never sees it.
        try:
            parsed = json.loads(arguments["edits"])
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            arguments = {**arguments, "edits": parsed}
            notes.append("used edits as a list (received it as text)")
    keys = set(arguments)
    if isinstance(arguments.get("files"), list) and keys & _PATCH_SIMPLE_FORM:
        return _patch_both_forms(arguments, notes)
    if (
        not {"path", "edits"} <= keys
        or not keys <= _PATCH_SINGLE_FILE
        or not isinstance(arguments["edits"], list)
    ):
        return arguments, notes
    row = {"path": arguments["path"], "edits": arguments["edits"]}
    if "expected_hash" in arguments:
        row["expected_hash"] = arguments["expected_hash"]
    return {"files": [row]}, [
        *notes,
        "used files=[{path, edits}] (received top-level path and edits)",
    ]


_PATCH_SIMPLE_FORM = frozenset({"path", "old_string", "new_string", "replace_all"})


def _patch_both_forms(arguments: dict, notes: list[str]) -> tuple[dict, list[str]]:
    """Both forms filled in (F41): `files` is the call; the simple form is dropped.

    Models that send the same edit twice (once per form) used to be refused
    until a loop guard stopped the turn. When the simple form repeats an edit
    already in `files`, nothing is lost; when it does not, the result says
    that it was left out, so the model can send it on its own.
    """
    simple = {key: arguments[key] for key in _PATCH_SIMPLE_FORM if key in arguments}
    kept = {key: value for key, value in arguments.items() if key not in _PATCH_SIMPLE_FORM}
    if "expected_hash" in kept and len(kept["files"]) == 1:
        # The top-level hash belongs to the one file of the batch.
        row = kept["files"][0]
        if isinstance(row, dict) and "expected_hash" not in row:
            kept["files"] = [{**row, "expected_hash": kept["expected_hash"]}]
        kept.pop("expected_hash")
    if _simple_edit_in_files(simple, kept["files"]):
        notes.append(
            "used files (path, old_string and new_string repeated an edit already in files)"
        )
    else:
        notes.append(
            "used files and left out the separate path/old_string/new_string, which did not "
            "match any edit in files; send that edit again on its own if it is still needed"
        )
    return kept, notes


def _simple_edit_in_files(simple: dict, files: list) -> bool:
    path, old, new = simple.get("path"), simple.get("old_string"), simple.get("new_string")
    if not (isinstance(path, str) and isinstance(old, str) and isinstance(new, str)):
        return False
    if simple.get("replace_all"):
        return False
    wanted = _same_path(path)
    for row in files:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            continue
        if _same_path(row["path"]) != wanted:
            continue
        for edit in row.get("edits") or ():
            if (
                isinstance(edit, dict)
                and edit.get("old_string") == old
                and edit.get("new_string") == new
            ):
                return True
    return False


def _same_path(path: str) -> str:
    import os

    return os.path.normcase(os.path.normpath(path.strip()))


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
