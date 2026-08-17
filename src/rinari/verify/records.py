"""Validation record kinds/results and persistence helpers (phase 3).

A validation record is the smallest unit of verification evidence: one
command (or manual check) of one kind, with a result. The completion gate
consumes the *latest* record per kind, so a failed run can always be
followed by a passing re-run without rewriting history.
"""

from __future__ import annotations

from typing import Any

from rinari.shared.clock import Clock, now_iso
from rinari.shared.errors import InvalidUsageError
from rinari.shared.ids import IdGenerator

VALIDATION_KINDS: tuple[str, ...] = (
    "test",
    "lint",
    "typecheck",
    "build",
    "schema",
    "manual",
    "custom",
)
VALIDATION_RESULTS: tuple[str, ...] = ("passed", "failed", "error", "skipped")

MAX_SUMMARY_LEN = 500
MAX_DETAIL_LEN = 8000
VALIDATION_ID_PREFIX = "val"


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def record_validation(
    repo,
    *,
    project_root: str,
    kind: str,
    result: str,
    command: str = "",
    summary: str = "",
    detail: str = "",
    artifact_ref: str | None = None,
    session_ref: str | None = None,
    clock: Clock,
    ids: IdGenerator,
) -> dict:
    kind = str(kind or "").strip().lower()
    result = str(result or "").strip().lower()
    if kind not in VALIDATION_KINDS:
        raise InvalidUsageError(
            f"Unknown validation kind {kind!r}",
            hint=f"Expected one of: {', '.join(VALIDATION_KINDS)}.",
        )
    if result not in VALIDATION_RESULTS:
        raise InvalidUsageError(
            f"Unknown validation result {result!r}",
            hint=f"Expected one of: {', '.join(VALIDATION_RESULTS)}.",
        )
    row = {
        "id": ids.new(VALIDATION_ID_PREFIX),
        "project_root": project_root,
        "session_ref": session_ref,
        "kind": kind,
        "command": str(command or "").strip(),
        "result": result,
        "summary": _clean_text(summary, MAX_SUMMARY_LEN)
        or f"{kind} {result} ({command.strip() or 'manual check'})",
        "detail": _clean_text(detail, MAX_DETAIL_LEN),
        "artifact_ref": artifact_ref,
        "created_at": None,
    }
    row["created_at"] = now_iso(clock)
    return repo.insert(row)


def latest_record(repo, project_root: str, kind: str) -> dict | None:
    return repo.latest(project_root, kind)


def latest_records(
    repo, project_root: str, *, kinds: tuple[str, ...] | None = None, limit: int = 50
) -> list[dict]:
    return repo.list(project_root, kinds=kinds, limit=limit)
