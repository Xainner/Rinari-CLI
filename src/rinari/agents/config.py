"""Per-agent model assignments (Phase 7).

Which model alias each built-in agent uses, plus an optional reasoning
effort override. Stored as one TOML file in the Rinari home so CLI and
desktop share it; resolution (alias → caller, tool capability checks,
fallback chain) lives in `agent_runtime.caller_for_agent`, and the effort
override flows through the existing `reasoning_effort` call path
(turns → AgentLoop → ModelRequest).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


@dataclass(frozen=True, slots=True)
class AgentAssignment:
    model: str | None = None
    fallback: str | None = None
    enabled: bool = True
    effort: str | None = None


def _coerce(value: Any) -> AgentAssignment:
    if not isinstance(value, dict):
        return AgentAssignment()
    model = value.get("model")
    fallback = value.get("fallback")
    enabled = value.get("enabled", True)
    effort = value.get("effort")
    return AgentAssignment(
        model=model if isinstance(model, str) and model else None,
        fallback=fallback if isinstance(fallback, str) and fallback else None,
        enabled=bool(enabled),
        effort=effort if effort in EFFORTS else None,
    )


class AgentConfigStore:
    """`~/.rinari/agents.toml`: `{[agent-name] model, fallback, enabled}`."""

    def __init__(self, home: Path) -> None:
        self._path = Path(home) / "agents.toml"

    def all(self) -> dict[str, AgentAssignment]:
        if not self._path.is_file():
            return {}
        try:
            raw = tomllib.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {name: _coerce(value) for name, value in raw.items() if isinstance(name, str)}

    def get(self, agent: str) -> AgentAssignment:
        return self.all().get(agent, AgentAssignment())

    def set(
        self,
        agent: str,
        *,
        model: str | None = None,
        fallback: str | None = None,
        enabled: bool | None = None,
        effort: Any = None,
        clear_effort: bool = False,
    ) -> AgentAssignment:
        current = self.all()
        previous = current.get(agent, AgentAssignment())
        if effort is None:
            resolved_effort = None if clear_effort else previous.effort
        elif effort in EFFORTS:
            resolved_effort = effort
        else:
            raise ValueError(f"Unknown effort {effort!r}; expected one of {EFFORTS}.")
        updated = AgentAssignment(
            model=model if model is not None else previous.model,
            fallback=fallback if fallback is not None else previous.fallback,
            enabled=enabled if enabled is not None else previous.enabled,
            effort=resolved_effort,
        )
        current[agent] = updated
        self._write(current)
        return updated

    def clear(self, agent: str) -> bool:
        current = self.all()
        if agent not in current:
            return False
        del current[agent]
        self._write(current)
        return True

    def _write(self, assignments: dict[str, AgentAssignment]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # TOML has no null: missing aliases persist as "" and coerce back.
        payload = {
            name: {
                "model": a.model or "",
                "fallback": a.fallback or "",
                "enabled": a.enabled,
                "effort": a.effort or "",
            }
            for name, a in sorted(assignments.items())
        }
        self._path.write_text(tomli_w.dumps(payload), encoding="utf-8")
