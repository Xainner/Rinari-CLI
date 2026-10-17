"""What the mother knows about her children (table `session_children`).

The lineage itself (parent, kind, brief) lives on the session row (#40); this
table keeps what only a delegated child has: where it works, its state and
its last report to the mother.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# start: the owner asked. offer: the owner accepted an offer. note and
# plan_step: the owner started a card Rinari left.
ORIGINS = ("start", "offer", "note", "plan_step")
# Origins Rinari opened herself: the mother goes on when they finish (A-8).
OPENED_BY_RINARI = ("start", "offer", "plan_step")
# At most this many children working at once per mother, and two levels.
ACTIVE_LIMIT = 4
DEPTH_LIMIT = 2
STATES = ("working", "completed", "failed", "cancelled", "waiting")
SUMMARY_CHARS = 400
PROMPT_REPORT_CHARS = 1500


@dataclass(slots=True)
class ChildRecord:
    child_session_id: str
    parent_session_id: str
    origin: str
    context: str = "brief"
    worktree_path: str | None = None
    branch: str | None = None
    base_commit: str | None = None
    external_content: bool = False
    state: str = "working"
    report: dict[str, Any] = field(default_factory=dict)
    reported: bool = True
    resolution: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "child_session_id": self.child_session_id,
            "parent_session_id": self.parent_session_id,
            "origin": self.origin,
            "context": self.context,
            "isolation": "worktree" if self.worktree_path else "same",
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "state": self.state,
            "report": self.report or None,
            "reported": self.reported,
            "resolution": self.resolution,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _from_row(row: Any) -> ChildRecord:
    report: dict[str, Any] = {}
    if row["report_json"]:
        try:
            report = json.loads(row["report_json"])
        except ValueError:
            report = {}
    return ChildRecord(
        child_session_id=row["child_session_id"],
        parent_session_id=row["parent_session_id"],
        origin=row["origin"],
        context=row["context"],
        worktree_path=row["worktree_path"],
        branch=row["branch"],
        base_commit=row["base_commit"],
        external_content=bool(row["external_content"]),
        state=row["state"],
        report=report,
        reported=bool(row["reported"]),
        resolution=row["resolution"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ChildStore:
    def __init__(self, db: Any) -> None:
        self._db = db

    def insert(self, record: ChildRecord) -> ChildRecord:
        now = _now()
        record.created_at = record.updated_at = now
        self._db.execute(
            "INSERT INTO session_children (child_session_id, parent_session_id, origin, context, "
            "worktree_path, branch, base_commit, external_content, state, reported, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                record.child_session_id,
                record.parent_session_id,
                record.origin,
                record.context,
                record.worktree_path,
                record.branch,
                record.base_commit,
                int(record.external_content),
                record.state,
                now,
                now,
            ),
        )
        return record

    def get(self, child_session_id: str) -> ChildRecord | None:
        row = self._db.query_one(
            "SELECT * FROM session_children WHERE child_session_id = ?", (child_session_id,)
        )
        return _from_row(row) if row is not None else None

    def of(self, parent_session_id: str) -> list[ChildRecord]:
        rows = self._db.query(
            "SELECT c.* FROM session_children c JOIN sessions s ON s.id = c.child_session_id "
            "WHERE c.parent_session_id = ? ORDER BY c.created_at, c.rowid",
            (parent_session_id,),
        )
        return [_from_row(row) for row in rows]

    def working(self, parent_session_id: str) -> list[ChildRecord]:
        return [c for c in self.of(parent_session_id) if c.state == "working"]

    def set_state(
        self, child_session_id: str, state: str, report: dict[str, Any] | None = None
    ) -> ChildRecord | None:
        if report is None:
            self._db.execute(
                "UPDATE session_children SET state = ?, updated_at = ? WHERE child_session_id = ?",
                (state, _now(), child_session_id),
            )
        else:
            self._db.execute(
                "UPDATE session_children SET state = ?, report_json = ?, reported = 0, "
                "updated_at = ? WHERE child_session_id = ?",
                (state, json.dumps(report, ensure_ascii=False), _now(), child_session_id),
            )
        return self.get(child_session_id)

    def unreported(self, parent_session_id: str) -> list[ChildRecord]:
        return [c for c in self.of(parent_session_id) if not c.reported]

    def mark_reported(self, child_session_ids: list[str]) -> None:
        for child_id in child_session_ids:
            self._db.execute(
                "UPDATE session_children SET reported = 1 WHERE child_session_id = ?",
                (child_id,),
            )

    def resolve(self, child_session_id: str, resolution: str) -> ChildRecord | None:
        self._db.execute(
            "UPDATE session_children SET resolution = ?, updated_at = ? WHERE child_session_id = ?",
            (resolution, _now(), child_session_id),
        )
        return self.get(child_session_id)

    def depth(self, session_id: str) -> int:
        """How many mothers are above this conversation (0 for a root)."""
        depth = 0
        current = session_id
        while depth <= DEPTH_LIMIT + 1:
            row = self._db.query_one(
                "SELECT parent_session_id FROM session_children WHERE child_session_id = ?",
                (current,),
            )
            if row is None:
                return depth
            depth += 1
            current = row["parent_session_id"]
        return depth

    def render_for_prompt(self, parent_session_id: str, titles: dict[str, str]) -> str | None:
        """The children the model should keep in mind, for the turn's task state."""
        rows = [c for c in self.of(parent_session_id) if c.state == "working" or not c.reported]
        if not rows:
            return None
        lines = [
            "Child sessions you opened (separate conversations; a report is a claim, check it):"
        ]
        for child in rows[-6:]:
            title = titles.get(child.child_session_id) or child.child_session_id
            where = f" · branch {child.branch}" if child.branch else ""
            if child.state == "working":
                lines.append(f"- «{title}» ({child.child_session_id}): working{where}")
                continue
            report = child.report or {}
            files = report.get("files") or []
            shown = ", ".join(files[:6]) + (f" (+{len(files) - 6})" if len(files) > 6 else "")
            # A report not seen yet goes in longer: the mother tells the owner from it.
            full = report.get("text") if not child.reported else None
            summary = (
                str(full).strip()[:PROMPT_REPORT_CHARS]
                if full
                else " ".join(str(report.get("summary") or "").split())[:SUMMARY_CHARS]
            )
            lines.append(
                f"- «{title}» ({child.child_session_id}): {child.state}{where}"
                + (f" · files: {shown}" if shown else "")
                + (f"\n  report: {summary}" if summary else "")
            )
        lines.append(
            "Follow up with session.message (only your own children); session.children for "
            "details. Merging a branch is the user's decision."
        )
        return "\n".join(lines)
