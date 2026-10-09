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

    def save(value, suffix):
        text = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        )
        ref = spill(suffix, text)
        artifacts.append(ref)
        return ref.uri

    # The complete observation, exactly as the model would have read it: JSON
    # for most tools, plain sections for command output.
    uri = save(result.to_model_text(tool), "complete")
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
        "recovery": {"tool": "artifact.read", "uri": uri, "start_byte": 0},
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


# Process output keeps its beginning and its end: the command's first lines
# say what ran, the last ones carry the summary, the error and the exit
# status. Every other text keeps its head (it continues with a cursor).
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
    head = units[: (count + 1) // 2]
    tail = units[len(units) - count // 2 :] if count // 2 else []
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
