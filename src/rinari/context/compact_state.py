"""Provider-independent compact state (harness.md sections 67-68).

Compaction must preserve task truth, not a free-form "conversation summary".
`CompactState` is the structured record of that truth: it can be persisted
in `sessions.compact_state_json`, rendered as a prompt segment, and rebuilt
on resume without any provider-specific thread/summary data.

Extraction is deliberately rule-based and conservative: every field is only
filled from evidence actually present in the conversation or in persisted
store state. Missing evidence stays empty; the harness never invents state.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any

from rinari.models.types import ROLE_ASSISTANT, ROLE_USER, ChatMessage

GOAL_MAX_CHARS = 300
CONSTRAINT_MAX_CHARS = 200
MAX_CONSTRAINTS = 10
MAX_LIST_ITEMS = 50

# A user message counts as a standing constraint when it uses imperative
# preference language (bilingual; conservative on purpose).
_CONSTRAINT_MARKERS: tuple[str, ...] = (
    "siempre",
    "nunca",
    "prefiero",
    "prefiero que",
    "debes",
    "debe ",
    "usar ",
    "usar solo",
    "always",
    "never",
    "i prefer",
    "prefer ",
    "you must",
    "you should always",
    "don't",
    "do not",
    "avoid ",
)

# Tool calls that mutate the working tree, and the argument name that names
# the target file for each.
_CHANGE_TOOLS: dict[str, str] = {
    "fs.write": "path",
    "fs.patch": "path",
    "fs.mkdir": "path",
    "fs.rename": "path",
    "fs.delete": "path",
}


@dataclass(frozen=True, slots=True)
class CompactState:
    goal: str = ""
    constraints: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    tasks_completed: tuple[str, ...] = ()
    tasks_active: tuple[str, ...] = ()
    tasks_blocked: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    validations: tuple[str, ...] = ()
    approvals: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    project_root: str = ""
    provider_model: str = ""
    compacted_at: str = ""

    def is_empty(self) -> bool:
        return not any(
            (
                self.goal,
                self.constraints,
                self.decisions,
                self.tasks_completed,
                self.tasks_active,
                self.tasks_blocked,
                self.changed_files,
                self.validations,
                self.approvals,
                self.blockers,
                self.artifacts,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CompactState:
        if not data:
            return cls()
        list_fields = (
            "constraints",
            "decisions",
            "tasks_completed",
            "tasks_active",
            "tasks_blocked",
            "changed_files",
            "validations",
            "approvals",
            "blockers",
            "artifacts",
        )
        kwargs: dict[str, Any] = {}
        for class_field in fields(cls):
            key = class_field.name
            if key in data:
                value = data[key]
                if key in list_fields:
                    value = tuple(str(v) for v in (value or ()))
                kwargs[key] = value
        return cls(**kwargs)

    def render_prompt(self) -> str:
        lines: list[str] = [
            "Compact state (harness): earlier conversation was compacted. This is "
            "the preserved task truth; details that are no longer verifiable "
            "here must be re-checked, not assumed."
        ]
        if self.goal:
            lines.append(f"Goal: {self.goal}")
        if self.constraints:
            lines.append("Constraints:")
            lines.extend(f"- {c}" for c in self.constraints)
        if self.decisions:
            lines.append("Decisions:")
            lines.extend(f"- {d}" for d in self.decisions)
        tasks = (
            [(f"- done: {t}") for t in self.tasks_completed]
            + [(f"- active: {t}") for t in self.tasks_active]
            + [(f"- blocked: {t}") for t in self.tasks_blocked]
        )
        if tasks:
            lines.append("Task graph:")
            lines.extend(tasks)
        if self.changed_files:
            lines.append("Changed files:")
            lines.extend(f"- {p}" for p in self.changed_files)
        if self.validations:
            lines.append("Validation evidence:")
            lines.extend(f"- {v}" for v in self.validations)
        if self.approvals:
            lines.append("Approvals granted:")
            lines.extend(f"- {a}" for a in self.approvals)
        if self.blockers:
            lines.append("Unresolved/blockers:")
            lines.extend(f"- {b}" for b in self.blockers)
        if self.artifacts:
            lines.append("Artifacts:")
            lines.extend(f"- {a}" for a in self.artifacts)
        if self.project_root:
            lines.append(f"Project root: {self.project_root}")
        if self.provider_model:
            lines.append(f"Provider/model: {self.provider_model}")
        return "\n".join(lines)


def extract_from_history(
    history: tuple[ChatMessage, ...] | list[ChatMessage],
) -> CompactState:
    """Rule-based extraction of goal/constraints/changed files from messages.

    Only conversation-visible facts; store-based evidence (tasks, validation,
    artifacts, approvals) is merged separately with `merge_evidence`.
    """
    goal = ""
    constraints: list[str] = []
    changed: list[str] = []
    seen_files: set[str] = set()

    for message in history:
        text = (message.content or "").strip()
        if message.role == ROLE_USER and text:
            if not goal:
                goal = text[:GOAL_MAX_CHARS]
            lowered = text.lower()
            has_marker = any(marker in lowered for marker in _CONSTRAINT_MARKERS)
            if has_marker and len(constraints) < MAX_CONSTRAINTS:
                constraints.append(text[:CONSTRAINT_MAX_CHARS])
        if message.role == ROLE_ASSISTANT:
            for call in message.tool_calls:
                arg_name = _CHANGE_TOOLS.get(call.name)
                if arg_name is None:
                    continue
                path = call.arguments.get(arg_name)
                if isinstance(path, str) and path and path not in seen_files:
                    seen_files.add(path)
                    if len(changed) < MAX_LIST_ITEMS:
                        changed.append(path)

    return CompactState(
        goal=goal,
        constraints=tuple(constraints),
        changed_files=tuple(changed),
    )


def merge_evidence(state: CompactState, evidence: dict[str, Any]) -> CompactState:
    """Overlay persisted store evidence (storage-aware layer) on the
    conversation-derived state. Evidence lists replace conversation-derived
    ones of the same kind; the goal never gets dropped."""
    return CompactState(
        goal=state.goal,
        constraints=state.constraints,
        decisions=tuple(str(v) for v in evidence.get("decisions", ()))[:MAX_LIST_ITEMS],
        tasks_completed=tuple(str(v) for v in evidence.get("tasks_completed", ()))[:MAX_LIST_ITEMS],
        tasks_active=tuple(str(v) for v in evidence.get("tasks_active", ()))[:MAX_LIST_ITEMS],
        tasks_blocked=tuple(str(v) for v in evidence.get("tasks_blocked", ()))[:MAX_LIST_ITEMS],
        changed_files=tuple(str(v) for v in evidence.get("changed_files", state.changed_files)),
        validations=tuple(str(v) for v in evidence.get("validations", ()))[:MAX_LIST_ITEMS],
        approvals=tuple(str(v) for v in evidence.get("approvals", ()))[:MAX_LIST_ITEMS],
        blockers=tuple(str(v) for v in evidence.get("blockers", ()))[:MAX_LIST_ITEMS],
        artifacts=tuple(str(v) for v in evidence.get("artifacts", ()))[:MAX_LIST_ITEMS],
        project_root=str(evidence.get("project_root") or ""),
        provider_model=str(evidence.get("provider_model") or ""),
        compacted_at=str(evidence.get("compacted_at") or ""),
    )


def state_to_json(state: CompactState) -> str:
    return json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True)


def state_from_json(raw: str | None) -> CompactState:
    if not raw:
        return CompactState()
    try:
        return CompactState.from_dict(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return CompactState()


__all__ = [
    "CompactState",
    "extract_from_history",
    "merge_evidence",
    "state_from_json",
    "state_to_json",
]
