"""agent.wait over several subagents waits for a result the model has not read
(plan 0.4.0 v2, V2-12, F38): once the first child was done, every wait came
back at once with that same result and the coordinator asked 25 times."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from rinari.agents.tools import AgentToolHost, agent_tools


def _result(name):
    return SimpleNamespace(
        agent=name,
        objective="o",
        status="completed",
        ok=True,
        summary=f"{name} done",
        evidence=(),
        files_changed=(),
        validation=None,
        patch=None,
        branch=None,
        commit=None,
        conflicts=(),
        error=None,
        usage=None,
        provenance=None,
    )


class Orchestrator:
    def __init__(self):
        self.results: dict = {}
        self.delivered: set = set()

    def result(self, agent_id):
        return self.results.get(agent_id)

    def status(self, agent_id):
        return {"state": "completed" if agent_id in self.results else "running"}

    def note_result_delivered(self, agent_id):
        if agent_id in self.results:
            self.delivered.add(agent_id)

    def result_delivered(self, agent_id):
        return agent_id in self.delivered


def test_a_second_wait_blocks_until_another_child_finishes() -> None:
    orch = Orchestrator()
    wait = next(t for t in agent_tools(AgentToolHost(orchestrator=orch)) if t.name == "agent.wait")
    ctx = SimpleNamespace(cancellation=None, deadline_at=None)
    orch.results["a"] = _result("a")

    first = wait.handler({"agent_ids": ["a", "b"], "timeout_s": 5}, ctx)
    assert first.data["timed_out"] is False
    assert first.data["agents"][0]["result"]["summary"] == "a done"

    threading.Timer(0.4, lambda: orch.results.__setitem__("b", _result("b"))).start()
    started = time.monotonic()
    second = wait.handler({"agent_ids": ["a", "b"], "timeout_s": 5}, ctx)
    assert time.monotonic() - started >= 0.3  # it waited for b, not a again
    assert second.data["agents"][0]["delivered_before"] is True
    assert second.data["agents"][1]["result"]["summary"] == "b done"

    third = wait.handler({"agent_ids": ["a", "b"], "timeout_s": 5}, ctx)
    assert third.data["timed_out"] is False  # all done: it returns at once
