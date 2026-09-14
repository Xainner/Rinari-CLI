"""Recoverable provider projections; serialization never discards evidence."""

from __future__ import annotations

import json
from dataclasses import replace

from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult


def project_result(result: ToolResult, *, tool: str, budget: int, spill, force=False) -> ToolResult:
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
        ref = spill(suffix, json.dumps(value, ensure_ascii=False, default=str))
        artifacts.append(ref)
        return ref.uri

    uri = save(json.loads(result.to_model_text(tool)), "complete")
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
    for key in ("path", "uri", "exit_code", "running", "sha256"):
        if key in data:
            projected[key] = data[key]
    slots = []

    def collect(source, target):
        for key in ("text", "stdout", "stderr", "lines", "entries", "matches", "value"):
            value = source.get(key)
            if isinstance(value, (str, list)):
                units = value.splitlines(keepends=True) if isinstance(value, str) else value
                target[key] = "" if isinstance(value, str) else []
                slots.append((target, key, units, isinstance(value, str)))

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
    candidate = replace(result, data=projected, artifacts=tuple(artifacts))
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
        for target, key, units, is_text in remaining:
            before = size(candidate)
            old = target[key]
            used = len(old.splitlines(keepends=True)) if is_text else len(old)
            low, high = used, len(units)
            while low < high:
                mid = (low + high + 1) // 2
                target[key] = "".join(units[:mid]) if is_text else units[:mid]
                if size(candidate) <= min(budget, before + share):
                    low = mid
                else:
                    high = mid - 1
            target[key] = "".join(units[:low]) if is_text else units[:low]
            advanced |= low > used
            if low < len(units):
                pending.append((target, key, units, is_text))
        if not advanced:
            break
        remaining = pending
    return candidate


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
