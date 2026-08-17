"""Task graph core: status rules, done-when contract, dependency checks (phase 3).

Pure functions over plain task dicts (no storage import): the service wires
the repository, the clock, and structured errors. Status machine:

    pending --start--> in_progress --evidence--> done
       |                     |
       +--block--> blocked --retry--> pending
       |                     |
       +-------------+-------+
       +-------cancel (from any state except done)
       +---------------------+
"""

from __future__ import annotations

import re
from collections.abc import Mapping

PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
BLOCKED = "blocked"
CANCELLED = "cancelled"

VALID_STATUSES = {PENDING, IN_PROGRESS, DONE, BLOCKED, CANCELLED}

_CRITERION = re.compile(r"^(?:[-*]\s*)?\[( |x|X)\]\s*(.*)$")


def criteria_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def parse_criteria(text: str) -> list[tuple[str, bool]]:
    """Parse a checklist. Plain lines count as unsatisfied (`- [ ]`)."""
    items: list[tuple[str, bool]] = []
    for line in criteria_lines(text):
        match = _CRITERION.match(line)
        if match:
            items.append((match.group(2).strip(), match.group(1).lower() == "x"))
        else:
            items.append((line, False))
    return items


def _group_report(items: list[tuple[str, bool]]) -> dict:
    total = len(items)
    satisfied = sum(1 for _, ok in items if ok)
    return {
        "total": total,
        "satisfied": satisfied,
        "ok": total > 0 and satisfied == total,
        "missing": [text for text, ok in items if not ok],
    }


def done_when_report(task: Mapping[str, object]) -> dict:
    """The done-when contract for a task.

    A task can complete only when:
      - acceptance criteria exist and are all satisfied (task-specific);
      - implementation criteria are all satisfied (vacuous if none);
      - validation criteria exist and are all satisfied;
      - scope criteria are all satisfied (vacuous if none);
      - no unresolved criteria are outstanding.
    """
    acceptance = parse_criteria(str(task.get("acceptance") or ""))
    implementation = parse_criteria(str(task.get("implementation") or ""))
    validation = parse_criteria(str(task.get("validation") or ""))
    scope = parse_criteria(str(task.get("scope") or ""))
    unresolved = criteria_lines(str(task.get("unresolved") or ""))

    acceptance_report = _group_report(acceptance)
    implementation_report = _group_report(implementation)
    validation_report = _group_report(validation)
    scope_report = _group_report(scope)

    implementation_ok = len(implementation) == 0 or all(ok for _, ok in implementation)
    scope_ok = len(scope) == 0 or all(ok for _, ok in scope)
    can_complete = (
        acceptance_report["ok"]
        and implementation_ok
        and validation_report["ok"]
        and scope_ok
        and not unresolved
    )
    return {
        "acceptance": acceptance_report,
        "implementation": implementation_report,
        "validation": validation_report,
        "scope": scope_report,
        "unresolved": unresolved,
        "can_complete": can_complete,
    }


def completion_blockers(task: Mapping[str, object]) -> list[str]:
    """Human-readable reasons a task cannot be marked done yet."""
    report = done_when_report(task)
    reasons: list[str] = []
    for group in ("acceptance", "implementation", "validation", "scope"):
        info = report[group]
        unsatisfied = info["total"] - info["satisfied"]
        if unsatisfied:
            reasons.append(f"{group}: {unsatisfied} criterion(s) unsatisfied")
    if not report["acceptance"]["total"]:
        reasons.append("acceptance: task-specific acceptance criteria missing")
    if not report["validation"]["total"]:
        reasons.append("validation: validation criteria missing")
    if report["unresolved"]:
        reasons.append(f"unresolved: {len(report['unresolved'])} outstanding")
    return reasons


def split_dependencies(task: Mapping[str, object]) -> list[str]:
    raw = str(task.get("depends_on") or "")
    return [dep.strip() for dep in raw.split(",") if dep.strip()]


def dependencies_ready(
    task: Mapping[str, object], by_id: Mapping[str, Mapping[str, object]]
) -> list[str]:
    """Dependency IDs that are not (satisfied = done)."""
    missing = []
    for dep in split_dependencies(task):
        target = by_id.get(dep)
        if target is None:
            missing.append(dep)
            continue
        if target.get("status") != DONE:
            missing.append(dep)
    return missing


def would_create_cycle(tasks: list[Mapping[str, object]], task_id: str, new_dep: str) -> bool:
    """True if adding `task_id -> new_dep` closes a dependency cycle.

    A cycle exists when `task_id` is reachable from `new_dep` by following
    existing depends_on edges.
    """
    if task_id == new_dep:
        return True
    edges = {t["id"]: split_dependencies(t) for t in tasks}
    seen: set[str] = set()
    frontier = [new_dep]
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(edges.get(current, ()))
    return task_id in seen


def tree_depths(tasks: list[Mapping[str, object]]) -> dict[str, int]:
    """Render depth per task for `tasks tree` (0 = root level)."""
    by_id = {t["id"]: t for t in tasks}
    depths: dict[str, int] = {}

    def depth(task_id: str, visiting: set[str]) -> int:
        if task_id in depths:
            return depths[task_id]
        if task_id in visiting:  # defensive: cycle already rejected at write time
            return 0
        task = by_id.get(task_id)
        if task is None:
            return 0
        visiting = visiting | {task_id}
        deps = [dep for dep in split_dependencies(task) if dep in by_id]
        value = 0 if not deps else max(depth(dep, visiting) for dep in deps) + 1
        depths[task_id] = value
        return value

    for task in tasks:
        depth(task["id"], set())
    return depths
