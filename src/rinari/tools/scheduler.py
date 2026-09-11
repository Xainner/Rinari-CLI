"""Tool-call scheduler (Etapa C / review §5.1-5.2).

Plans each model turn's tool calls into ordered groups. Calls inside one
group are safe to run together (read-only + idempotent + independent);
groups themselves always run in order, and observations are reported back
in the original call order (§5.2 determinism).

Execution today is still serial within the loop (baseline correctness for
policy/approval/event ordering); the plan is computed, traced, and ready
for a concurrent executor once the runtime's shared state is hardened.
"""

from __future__ import annotations

from typing import Any

from rinari.tools.definition import SIDE_EFFECT_NONE


def is_parallelizable(tool: Any, arguments: dict | None = None) -> bool:
    """READ_ONLY + idempotent + independent resources → may share a group."""
    if tool is None:
        return False
    if getattr(tool, "side_effects", None) != SIDE_EFFECT_NONE:
        return False
    if not bool(getattr(tool, "idempotent", False)):
        return False
    try:
        actions = tool.classify_actions(arguments or {})
    except Exception:
        return False
    return all(action.capability != "network.outbound" for action in actions)


def schedule(
    names: list[str], registry: Any, arguments: list[dict] | None = None
) -> list[list[str]]:
    """Group consecutive parallelizable calls; everything else runs alone."""
    groups: list[list[str]] = []
    current: list[str] = []
    for index, name in enumerate(names):
        tool = registry.get(name)
        if tool is not None and is_parallelizable(
            tool, arguments[index] if arguments is not None else None
        ):
            current.append(name)
            continue
        if current:
            groups.append(current)
            current = []
        groups.append([name])
    if current:
        groups.append(current)
    return groups
