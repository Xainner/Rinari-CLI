"""AgentOrchestrator: spawn/wait/cancel/synthesize for subagents (phase 6).

The main agent stays the coordinator (harness.md 111). The orchestrator
owns the concurrency/depth/total limits (harness.md 115), per-agent
cancellation propagation, structured `AgentResult`s, and the synthesis
report (duplicate-work and contradiction detection). Execution is delegated
to an injected `SubagentRunner` so this module stays deterministic:
production wiring builds a scoped AgentLoop per subagent; tests inject a
scripted runner.
"""

from __future__ import annotations

import contextlib
import itertools
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from rinari.agents.definition import (
    MAX_CONCURRENT,
    MAX_DEPTH,
    MAX_TOTAL,
    AgentBudget,
    AgentDefinition,
)
from rinari.runtime.cancellation import CancellationToken

_terminal_states = ("completed", "failed", "cancelled", "budget")

# Result states that count as successful for a task-graph join.
_SUCCESS_STATES = ("completed",)


class OrchestratorError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        # AGENT_NOT_FOUND | AGENT_NOT_TERMINAL | LIMIT_TOTAL | LIMIT_CONCURRENT
        # | LIMIT_DEPTH | AGENT_DEFINITION
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Structured subagent output; evidence, never a trusted instruction."""

    agent: str
    objective: str
    status: str  # completed | failed | cancelled | budget
    summary: str
    evidence: tuple[str, ...] = ()
    files_changed: tuple[str, ...] = ()
    validation: dict = field(default_factory=dict)
    patch: str = ""
    branch: str | None = None
    commit: str | None = None
    conflicts: tuple[str, ...] = ()
    error: str = ""
    usage: dict = field(default_factory=dict)
    provenance: str = ""

    @property
    def ok(self) -> bool:
        return self.status in _SUCCESS_STATES


@dataclass
class SubagentRunSpec:
    agent_id: str
    definition: AgentDefinition
    objective: str
    context: dict[str, str] = field(default_factory=dict)
    project_root: Path | None = None
    session_id: str = ""
    depth: int = 1
    budget: AgentBudget = field(default_factory=AgentBudget)
    token: CancellationToken = field(default_factory=CancellationToken)
    worktree: Any | None = None  # WorktreeInfo | None (writers only)
    messages: queue.Queue[str] = field(default_factory=queue.Queue)


class SubagentRunner(Protocol):
    def run(self, spec: SubagentRunSpec) -> AgentResult: ...


def extract_evidence(text: str) -> list[str]:
    import re

    refs: list[str] = []
    for m in re.finditer(r"artifact://[^\s`\"']+", text):
        refs.append(m.group(0))
    for m in re.finditer(
        r"([A-Za-z0-9_./\\-]+\.(?:py|ts|js|json|md|ya?ml|toml|sql|go|rs|sh)):([0-9]+)", text
    ):
        ref = f"{m.group(1)}:{m.group(2)}"
        if ref not in refs:
            refs.append(ref)
    return refs[:50]


def parse_validation_block(text: str) -> dict:
    """Best-effort parse of a ```json validation block the agent reported."""
    import json
    import re

    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    validation = {k: data[k] for k in ("tests", "lint", "typecheck", "build") if k in data}
    for key, value in data.items():
        if (
            key not in validation
            and isinstance(value, str)
            and value
            in (
                "passed",
                "failed",
                "skipped",
                "unknown",
            )
        ):
            validation[key] = value
    return validation


class AgentOrchestrator:
    def __init__(
        self,
        runner: SubagentRunner,
        *,
        registry: Any | None = None,
        worktrees: Any | None = None,
        task_service: Any | None = None,
        task_path: str | Path | None = None,
        max_concurrent: int = MAX_CONCURRENT,
        max_depth: int = MAX_DEPTH,
        max_total: int = MAX_TOTAL,
        event_sink: Callable[[str, str, dict], None] | None = None,
        clock: Any | None = None,
    ) -> None:
        self._runner = runner
        self._registry = registry
        self._worktrees = worktrees
        self._task_service = task_service
        self._task_path = task_path
        self.max_concurrent = max_concurrent
        self.max_depth = max_depth
        self.max_total = max_total
        self._event_sink = event_sink
        self._clock = clock
        self._agents: dict[str, dict] = {}
        self._spawned_total = 0
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._session_id = ""
        self._project_root_value: Path | None = None

    def bind_session(self, session_id: str) -> None:
        self._session_id = session_id

    # -- control plane -----------------------------------------------------------

    def spawn(
        self,
        agent: str,
        objective: str,
        *,
        context: dict[str, str] | None = None,
        task_id: str | None = None,
        use_worktree: bool = False,
        depth: int = 1,
        timeout_s: float = 600.0,
    ) -> str:
        with self._lock:
            if self._spawned_total >= self.max_total:
                raise OrchestratorError(
                    "LIMIT_TOTAL",
                    f"max_total agents reached ({self.max_total}) — no recursive agent explosion",
                )
            running = self._count_running()
            if running >= self.max_concurrent:
                raise OrchestratorError(
                    "LIMIT_CONCURRENT",
                    f"max_concurrent agents reached ({self.max_concurrent})",
                )
            if depth >= self.max_depth:
                raise OrchestratorError("LIMIT_DEPTH", f"max_depth reached ({self.max_depth})")
            self._spawned_total += 1

        definition = self._resolve_definition(agent)
        objective = str(objective or definition.objective).strip()
        if not objective:
            raise OrchestratorError("AGENT_DEFINITION", "empty objective")

        agent_id = f"agt_{next(self._ids):03d}"
        token = CancellationToken()
        spec = SubagentRunSpec(
            agent_id=agent_id,
            definition=definition,
            objective=objective,
            context=dict(context or {}),
            project_root=self._project_root(),
            session_id=self._session_id,
            depth=depth,
            budget=definition.budget,
            token=token,
            worktree=self._prepare_worktree(definition, agent_id, use_worktree),
        )
        state = {
            "id": agent_id,
            "agent": agent,
            "objective": objective,
            "state": "spawning",
            "result": None,
            "task_id": task_id,
            "definition": definition,
            "spec": spec,
            "token": token,
            "messages": spec.messages,
            "started_at": time.monotonic(),
            "timeout_s": timeout_s,
            "thread": None,
            "error": "",
        }
        with self._lock:
            self._agents[agent_id] = state

        self._emit_hook("SubagentStart", self._hook_payload(state))
        thread = threading.Thread(target=self._run_agent, args=(agent_id,), daemon=True)
        with self._lock:
            state["thread"] = thread
            state["state"] = "running"
        thread.start()
        return agent_id

    def status(self, agent_id: str) -> dict:
        state = self._state(agent_id)
        return self._status_dict(state)

    def list(self) -> list[dict]:
        with self._lock:
            return [self._status_dict(s) for s in self._agents.values()]

    def wait(self, agent_id: str, timeout_s: float | None = None) -> AgentResult:
        state = self._state(agent_id)
        thread = state["thread"]
        if thread is not None:
            thread.join(timeout=timeout_s)
        if state["state"] not in _terminal_states:
            if (
                state["state"] == "running"
                and time.monotonic() - state["started_at"] > state["timeout_s"]
            ):
                self.cancel(agent_id, reason="timeout")
            if state["state"] not in _terminal_states:
                raise OrchestratorError(
                    "AGENT_NOT_TERMINAL", f"agent {agent_id} still {state['state']}"
                )
        assert state["result"] is not None
        return state["result"]

    def cancel(self, agent_id: str, reason: str = "user") -> bool:
        state = self._state(agent_id)
        if state["state"] in _terminal_states:
            return False
        state["token"].cancel()
        state["state"] = f"cancelling ({reason})"
        return True

    def result(self, agent_id: str) -> AgentResult | None:
        state = self._state(agent_id)
        return state["result"]

    def message(self, agent_id: str, text: str) -> bool:
        state = self._state(agent_id)
        if state["state"] == "running":
            state["messages"].put(str(text))
            return True
        if state["state"] in _terminal_states:
            raise OrchestratorError("AGENT_NOT_TERMINAL", f"agent {agent_id} is {state['state']}")
        return False

    # -- synthesis -----------------------------------------------------------------

    def synthesize(self, agent_ids: list[str]) -> dict:
        results: list[AgentResult] = []
        for aid in agent_ids:
            try:
                results.append(self.wait(aid))
            except OrchestratorError:
                state = self._state(aid)
                if state["result"] is not None:
                    results.append(state["result"])
        duplicate_work = self._duplicate_work(results)
        contradictions = self._contradictions(results)
        tasks_to_complete = self._task_join(agent_ids)
        return {
            "agents": [
                {
                    "agent": r.agent,
                    "status": r.status,
                    "ok": r.ok,
                    "summary": r.summary,
                    "evidence": list(r.evidence),
                    "files_changed": list(r.files_changed),
                    "validation": r.validation,
                    "usage": r.usage,
                    "error": r.error,
                    "provenance": r.provenance,
                }
                for r in results
            ],
            "duplicate_work": duplicate_work,
            "contradictions": contradictions,
            "tasks_to_complete": tasks_to_complete,
            "all_ok": all(r.ok for r in results) and not contradictions,
        }

    # -- internals -------------------------------------------------------------------

    def _resolve_definition(self, agent: str) -> AgentDefinition:
        if self._registry is not None:
            definition = self._registry.get(agent, self._project_root())
            if definition is None:
                known = ", ".join(sorted(self._registry.list(self._project_root())))
                raise OrchestratorError(
                    "AGENT_DEFINITION", f"unknown agent {agent!r}; known: {known}"
                )
            return definition
        from rinari.agents.definition import BUILTIN_AGENTS

        if agent not in BUILTIN_AGENTS:
            raise OrchestratorError("AGENT_DEFINITION", f"unknown agent {agent!r}")
        return BUILTIN_AGENTS[agent]

    def _project_root(self) -> Path | None:
        return getattr(self, "_project_root_value", None)

    def set_project_root(self, root: Path | None) -> None:
        self._project_root_value = root

    def _count_running(self) -> int:
        return sum(1 for s in self._agents.values() if s["state"] not in _terminal_states)

    def _state(self, agent_id: str) -> dict:
        with self._lock:
            state = self._agents.get(agent_id)
        if state is None:
            raise OrchestratorError("AGENT_NOT_FOUND", f"unknown agent: {agent_id}")
        return state

    def _prepare_worktree(self, definition: AgentDefinition, agent_id: str, requested: bool):
        writer = definition.profile == "workspace"
        if not (writer and requested and self._worktrees is not None and self._project_root()):
            return None
        try:
            info = self._worktrees.create(agent_id)
        except Exception:
            return None
        return info

    def _run_agent(self, agent_id: str) -> None:
        state = self._agents[agent_id]
        try:
            result = self._runner.run(state["spec"])
        except Exception as exc:
            result = AgentResult(
                agent=state["agent"],
                objective=state["objective"],
                status="failed",
                summary="",
                error=f"runner crashed: {exc}",
                provenance=state["definition"].provenance,
            )
        with self._lock:
            state["result"] = result
            state["state"] = result.status if result.status in _terminal_states else "failed"
        self._emit_hook("SubagentStop", self._hook_payload(state))
        self._post_complete(agent_id)

    def _post_complete(self, agent_id: str) -> None:
        # No auto task-join here: a concurrent thread can finish before a
        # sibling agent for the same task is even registered, which would
        # complete the task prematurely. Joins are evaluated explicitly at
        # synthesize() time — the coordinator's report point (harness 114).
        return None

    def _task_join(self, agent_ids: list[str]) -> list[str]:
        """Mark owned tasks done when every agent of the task succeeded.

        The trigger set (agent_ids) only selects which tasks to check; the
        decision considers ALL agents assigned to each task, and validation
        contradictions among them block the auto-join (the coordinator
        arbitrates conflicts manually).
        """
        if self._task_service is None or self._task_path is None:
            return []
        completed: list[str] = []
        with self._lock:
            owned: set[str] = set()
            for aid in agent_ids:
                state = self._agents.get(aid)
                if state is not None and state["task_id"] is not None:
                    owned.add(state["task_id"])
            members: dict[str, list[str]] = {}
            for state in self._agents.values():
                if state["task_id"] in owned:
                    members.setdefault(state["task_id"], []).append(state["id"])
            snapshot = {
                task_id: [self._agents[aid] for aid in aids] for task_id, aids in members.items()
            }
        for task_id, states in snapshot.items():
            if any(state["state"] not in _SUCCESS_STATES for state in states):
                continue
            validations = [state["result"].validation or {} for state in states]
            if self._conflicting_checks(validations):
                continue
            try:
                self._task_service.update(self._task_path, task_id, status="done")
                completed.append(task_id)
            except Exception:
                continue
        return completed

    @staticmethod
    def _conflicting_checks(validations: list[dict]) -> bool:
        by_check: dict[str, set[str]] = {}
        for validation in validations:
            for key, value in validation.items():
                if value in ("passed", "failed"):
                    by_check.setdefault(key, set()).add(value)
        return any("passed" in values and "failed" in values for values in by_check.values())

    def _status_dict(self, state: dict) -> dict:
        result = state["result"]
        return {
            "id": state["id"],
            "agent": state["agent"],
            "objective": state["objective"],
            "state": state["state"],
            "task_id": state["task_id"],
            "profile": state["definition"].profile,
            "worktree": state["spec"].worktree.branch if state["spec"].worktree else None,
            "elapsed_s": round(time.monotonic() - state["started_at"], 1),
            "error": state["error"] or (result.error if result else ""),
            "summary": result.summary if result else None,
        }

    def _hook_payload(self, state: dict) -> dict:
        result = state["result"]
        return {
            "agent_id": state["id"],
            "agent": state["agent"],
            "objective": state["objective"][:200],
            "session_id": self._session_id,
            "state": state["state"],
            "status": result.status if result else None,
        }

    def _emit_hook(self, event: str, payload: dict) -> None:
        if self._event_sink is None:
            return
        with contextlib.suppress(Exception):
            # hooks are observability, not a functional dependency
            self._event_sink(self._session_id, event, payload)

    # -- synthesis helpers -------------------------------------------------------------

    @staticmethod
    def _duplicate_work(results: list[AgentResult]) -> list[dict]:
        seen: dict[str, list[str]] = {}
        for r in results:
            for f in r.files_changed:
                seen.setdefault(f, []).append(r.agent)
        return [
            {"file": f, "agents": sorted(set(agents))}
            for f, agents in sorted(seen.items())
            if len(set(agents)) > 1
        ]

    @staticmethod
    def _contradictions(results: list[AgentResult]) -> list[dict]:
        found: list[dict] = []
        keys: dict[str, dict[str, list[str]]] = {}
        for r in results:
            for key, value in (r.validation or {}).items():
                keys.setdefault(key, {}).setdefault(str(value), []).append(r.agent)
        for key, values in sorted(keys.items()):
            normalized = {v for v in values if v in ("passed", "failed")}
            if "passed" in normalized and "failed" in normalized:
                found.append(
                    {
                        "check": key,
                        "passed_by": sorted(values.get("passed", [])),
                        "failed_by": sorted(values.get("failed", [])),
                    }
                )
        return found


__all__ = [
    "AgentOrchestrator",
    "AgentResult",
    "OrchestratorError",
    "SubagentRunSpec",
    "SubagentRunner",
    "extract_evidence",
    "parse_validation_block",
]
