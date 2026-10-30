"""Recoverable provider projections; serialization never discards evidence."""

from __future__ import annotations

import json
from dataclasses import replace

from rinari.tools.definition import (
    COMMAND_OUTPUT_TOOLS,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

# Strings shorter than this are never judged: a stray control is not garbage.
_UNREADABLE_MIN_CHARS = 512
_UNREADABLE_SAMPLE_CHARS = 8192


def _unreadable(text: str) -> bool:
    """Binary decoded as text: replacement characters and C0 controls."""
    if len(text) < _UNREADABLE_MIN_CHARS:
        return False
    sample = text[:_UNREADABLE_SAMPLE_CHARS]
    bad = sum(
        1
        for ch in sample
        if ch == "\ufffd" or ch == "\x00" or (ch < " " and ch not in "\t\n\r\x0c\x1b")
    )
    return bad / len(sample) > 0.1


def omit_unreadable(result: ToolResult, *, spill) -> ToolResult:
    """Keep binary-as-text out of the model's context (plan 0.4.0 #61b).

    A string field that is not readable text (bytes decoded as text) is saved
    whole as evidence and replaced by a one-line notice with its reference, so
    it never travels in every following model call.
    """
    if not isinstance(result.data, dict):
        return result
    artifacts = list(result.artifacts)
    changed = False

    def clean(value, path):
        nonlocal changed
        if isinstance(value, str) and _unreadable(value):
            ref = spill(f"unreadable-{path}", value)
            artifacts.append(ref)
            changed = True
            return (
                f"[unreadable binary content omitted: {len(value)} characters; "
                f"saved as {ref.uri}. It is not text: do not read it again as text.]"
            )
        if isinstance(value, dict):
            return {key: clean(item, f"{path}-{key}") for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item, f"{path}-{index}") for index, item in enumerate(value)]
        return value

    data = clean(result.data, "data")
    if not changed:
        return result
    return replace(result, data=data, artifacts=tuple(artifacts))


def project_result(result: ToolResult, *, tool: str, budget: int, spill, force=False) -> ToolResult:
    """Fit ``result`` into ``budget`` bytes without discarding evidence.

    ``ToolResult.truncated`` means "the model did not observe the complete
    result": it is set whenever the delivery is partial, so activity and
    desktop presentation report the observation as incomplete even though the
    full evidence stays recoverable through ``artifact.read``.
    """

    def size(value):
        return len(value.to_model_text(tool).encode("utf-8"))

    if not force and size(result) <= budget:
        return result
    data = result.data if isinstance(result.data, dict) else {"value": result.data}
    # An artifact page already has immutable backing. Repage it directly; never
    # create artifacts containing references to further artifacts.
    if tool == "artifact.read" and isinstance(data.get("text"), str):
        text = data["text"]

        def page(count):
            end = data["start_byte"] + len(text[:count].encode("utf-8"))
            return replace(
                result,
                data={
                    **data,
                    "text": text[:count],
                    "end_byte": end,
                    "next_start_byte": end if end < data["size_bytes"] else None,
                    "truncated": end < data["size_bytes"],
                    "delivery_partial": count < len(text),
                },
                truncated=result.truncated or count < len(text),
            )

        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if size(page(mid)) <= budget:
                low = mid
            else:
                high = mid - 1
        if low or not text:
            return page(low)
        return replace(
            result,
            ok=False,
            data={"uri": data["uri"], "start_byte": data["start_byte"]},
            error=ToolErrorInfo(
                ToolErrorCode.RESOURCE_EXHAUSTED,
                "Increase the observation budget to read this artifact page",
            ),
        )

    artifacts = list(result.artifacts)

    def save(value, suffix, extension=".txt"):
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, default=str, indent=2)
        )
        ref = spill(suffix, text) if extension == ".txt" else spill(suffix, text, extension)
        artifacts.append(ref)
        return ref.uri

    # Plan 0.4.0 #76: the complete output is saved as the text inside it,
    # with real line breaks, so fs.read_lines and fs.search_text page it like
    # any file. The call's other fields go beside it as JSON.
    plain = None if isinstance(data.get("files"), list) else _plain_text(data)
    if plain is not None:
        text, extension = plain
        uri = save(text, "complete", extension)
        meta = {key: value for key, value in data.items() if key not in _TEXT_KEYS}
        if meta:
            save({"tool": tool, "ok": result.ok, "data": meta}, "meta", ".json")
        lines = text.count("\n") + (0 if text.endswith("\n") else 1)
        size_label = _size_label(len(text.encode("utf-8")))
    else:
        uri = save({"tool": tool, "ok": result.ok, "data": result.data}, "complete", ".json")
        lines = None
        size_label = None
    described = f" ({size_label}, {lines} lines)" if size_label else ""
    projected = {
        "result_ref": uri,
        "delivery_partial": True,
        "source_partial": bool(
            result.truncated
            or data.get("truncated")
            or any(
                data.get(key) is not None for key in ("next_line", "next_offset", "next_start_byte")
            )
        ),
        "recovery": {"tool": "fs.read_lines", "uri": uri, "path": uri, "start": 1},
        "note": (
            f"Output too large to show whole{described}. The full text is saved as {uri}: "
            f'page it with fs.read_lines {{"path": "{uri}", "start": 1}} or search it with '
            "fs.search_text; do not run the call again to see it."
        ),
    }
    kept = ("path", "uri", "exit_code", "running", "sha256")
    if tool in COMMAND_OUTPUT_TOOLS:
        # A failed command's text reports where and how it ran.
        kept += ("command", "cwd", "timeout", "hint")
    for key in kept:
        if key in data:
            projected[key] = data[key]
    slots = []

    def collect(source, target):
        for key in ("text", "stdout", "stderr", "lines", "entries", "matches", "value"):
            value = source.get(key)
            if isinstance(value, (str, list)):
                is_text = isinstance(value, str)
                ends = is_text and key in HEAD_TAIL_KEYS
                if ends:
                    units = _line_units(value)
                elif is_text:
                    units = value.splitlines(keepends=True)
                else:
                    units = value
                target[key] = "" if is_text else []
                # [target, key, units, is_text, head_and_tail, units_used]
                slots.append([target, key, units, is_text, ends, 0])

    files = data.get("files")
    if isinstance(files, list) and all(isinstance(row, dict) for row in files):
        projected["files"] = []
        for index, row in enumerate(files):
            target = {
                "path": row.get("path"),
                "ok": row.get("ok"),
                "error": row.get("error"),
                "result_ref": save(row, f"file-{index}"),
                "delivery_partial": True,
            }
            projected["files"].append(target)
            content = row.get("data")
            collect(content if isinstance(content, dict) else {"value": content}, target)
        projected["file_count"] = len(files)
    else:
        collect(data, projected)
    candidate = replace(result, data=projected, artifacts=tuple(artifacts), truncated=True)
    if size(candidate) > budget:
        # Full evidence is durable even when mandatory batch metadata cannot fit.
        return replace(
            candidate,
            ok=False,
            data={"result_ref": uri, "delivery_partial": True},
            artifacts=(artifacts[len(result.artifacts)],),
            error=ToolErrorInfo(
                ToolErrorCode.RESOURCE_EXHAUSTED,
                "Observation budget cannot hold required result references; "
                "increase runtime.tools observation budgets",
            ),
        )
    # Water filling by serialized bytes: small fields release their unused share.
    remaining = list(slots)
    while remaining:
        share = max(0, (budget - size(candidate)) // len(remaining))
        advanced = False
        pending = []
        for slot in remaining:
            target, key, units, is_text, ends, used = slot
            before = size(candidate)
            low, high = used, len(units)
            while low < high:
                mid = (low + high + 1) // 2
                target[key] = _excerpt(units, mid, is_text, ends)
                if size(candidate) <= min(budget, before + share):
                    low = mid
                else:
                    high = mid - 1
            target[key] = _excerpt(units, low, is_text, ends)
            slot[5] = low
            advanced |= low > used
            if low < len(units):
                pending.append(slot)
        if not advanced:
            break
        remaining = pending
    return candidate


_TEXT_KEYS = ("stdout", "stderr", "text", "markdown", "body", "content", "value")


def _plain_text(data: dict) -> tuple[str, str] | None:
    """The readable text inside a result and its file extension, or None."""
    sections: list[tuple[str, str]] = []
    for key in _TEXT_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value:
            sections.append((key, value))
        elif key == "content" and isinstance(value, list):
            # MCP: a list of content blocks; their text is what was said.
            texts = [
                item["text"]
                for item in value
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            if texts:
                sections.append((key, "\n\n".join(texts)))
    if not sections:
        return None
    if len(sections) == 1:
        key, text = sections[0]
        head = text.lstrip()[:512].lower()
        if key == "markdown":
            return text, ".md"
        if head.startswith(("<!doctype html", "<html")):
            return text, ".html"
        if head.startswith(("{", "[")):
            try:
                json.loads(text)
            except ValueError:
                return text, ".txt"
            return text, ".json"
        return text, ".txt"
    parts = []
    for key, text in sections:
        parts.append(f"===== {key} =====\n{text}" + ("" if text.endswith("\n") else "\n"))
    return "".join(parts), ".txt"


def _size_label(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{max(1, round(size / 1024))} KB"


# Process output keeps its beginning and its end: the command's first lines
# say what ran, the last ones carry the summary, the error and the exit
# status (40 % head, 60 % tail: errors gather at the end). Every other text
# keeps its head (it continues with a cursor).
HEAD_TAIL_KEYS = frozenset({"stdout", "stderr"})
# A single huge line (minified JSON, a progress bar without newlines) must not
# make the excerpt all-or-nothing.
_MAX_UNIT_CHARS = 2000


def _line_units(text: str) -> list[str]:
    units = []
    for line in text.splitlines(keepends=True):
        units.extend(line[i : i + _MAX_UNIT_CHARS] for i in range(0, len(line), _MAX_UNIT_CHARS))
    return units


def _excerpt(units, count, is_text, head_and_tail):
    if not is_text:
        return units[:count]
    if not head_and_tail or count >= len(units):
        return "".join(units[:count])
    if not count:
        return ""
    head_count = max(1, (count * 2 + 4) // 5)
    tail_count = count - head_count
    head = units[:head_count]
    tail = units[len(units) - tail_count :] if tail_count else []
    omitted = sum(len(unit.encode("utf-8")) for unit in units[len(head) : len(units) - len(tail)])
    joined = "".join(head)
    if joined and not joined.endswith("\n"):
        joined += "\n"
    return joined + f"[... {omitted} bytes omitted ...]\n" + "".join(tail)


def allocate_budgets(sizes, per_result, per_round):
    """Max-min fair allocation; completed small results release their share."""
    budgets = [0] * len(sizes)
    remaining = per_round
    pending = set(range(len(sizes)))
    while pending:
        share = remaining // len(pending)
        small = [i for i in pending if min(sizes[i], per_result) <= share]
        if not small:
            for i in sorted(pending):
                budgets[i] = min(per_result, share)
            break
        for i in small:
            budgets[i] = min(sizes[i], per_result)
            remaining -= budgets[i]
            pending.remove(i)
    return budgets
