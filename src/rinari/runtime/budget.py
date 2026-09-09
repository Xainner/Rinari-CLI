"""Turn budgets (phase 4): metered, enforced, observable per-turn limits.

Dimensions:

    model calls      bounded loop iterations
    tool calls       bounded tool executions per turn
    network calls    tools whose namespace is network-facing
    wall time        real elapsed time via the injected Clock
    cost             estimated from actual token usage x pricing info;
                     enforced only when pricing is provided — cost is never
                     invented (harness observability rule)
    subagents        subagent invocation counter; no subagents spawn in
                     phase 4, but the limit and meter exist so the later
                     multi-agent runtime enforces the same dimension
    recursion depth  maximum subagent nesting observed

A `BudgetMeter` accumulates usage; `TurnBudgetLimits` declares ceilings.
`exhausted()` returns the names of the dimensions at ceiling (priority
ordered) so the loop can stop the turn with an accurate, machine-readable
reason instead of a generic failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rinari.models.types import Usage
from rinari.shared.clock import Clock

MODEL_CALLS = "model-calls"
TOOL_CALLS = "tool-calls"
NETWORK_CALLS = "network-calls"
WALL_TIME = "wall-time"
COST = "cost"
SUBAGENTS = "subagents"
RECURSION_DEPTH = "recursion-depth"

# Dimensions are reported in this order when several are exhausted at once.
PRIORITY = (MODEL_CALLS, TOOL_CALLS, NETWORK_CALLS, SUBAGENTS, RECURSION_DEPTH, COST, WALL_TIME)

# Tool namespaces that touch the network (web/http/browser land in phase 5;
# the meter already counts them as soon as they exist).
DEFAULT_NETWORK_NAMESPACES = frozenset({"web", "http", "browser"})


@dataclass(frozen=True, slots=True)
class TurnBudgetLimits:
    # None means no ordinary task budget. The large defaults that remain are
    # emergency circuit breakers, not a target amount of work.
    max_model_calls: int | None = 500
    max_tool_calls: int | None = 5000
    max_network_calls: int | None = None
    max_wall_time_s: float | None = 7200.0
    # None = no cost limit. Pricing is per-million tokens; None = unknown
    # (then cost is unmeasured and can never exhaust the budget).
    max_cost: float | None = None
    input_price_per_mtok: float | None = None
    output_price_per_mtok: float | None = None
    max_subagent_calls: int | None = 100
    max_recursion_depth: int | None = 8


def _default_is_network(name: str) -> bool:
    return name.split(".", 1)[0] in DEFAULT_NETWORK_NAMESPACES


@dataclass(slots=True)
class BudgetMeter:
    limits: TurnBudgetLimits
    clock: Clock
    is_network: Callable[[str], bool] | None = None
    # Hierarchical ledger (P0.10): a child meter forwards every note to its
    # parent, so the spawning turn's snapshot reflects real aggregate cost
    # instead of main-agent-only spend. Gates always apply to the local
    # counters; the parent's own gates see the forwarded totals.
    parent: BudgetMeter | None = None
    model_calls: int = 0
    tool_calls: int = 0
    network_calls: int = 0
    subagent_calls: int = 0
    max_recursion_depth: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    started_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.started_at = self.clock.now()

    def spawn_child(self, limits: TurnBudgetLimits | None = None, *, depth: int = 1) -> BudgetMeter:
        """Create a bounded child ledger attached to this meter.

        Counts the spawn itself (with the child's depth) and returns a
        meter whose notes forward upward with relativized depth.
        """
        child = BudgetMeter(
            limits or self.limits,
            clock=self.clock,
            is_network=self.is_network,
            parent=self,
        )
        self.note_subagent(depth)
        return child

    @property
    def _net(self) -> Callable[[str], bool]:
        return self.is_network or _default_is_network

    # -- recording -----------------------------------------------------------

    def note_model_call(self) -> None:
        self.model_calls += 1
        if self.parent is not None:
            self.parent.note_model_call()

    def note_tool_call(self, name: str, *, is_network: bool | None = None) -> None:
        self.tool_calls += 1
        network = is_network if is_network is not None else self._net(name)
        if network:
            self.network_calls += 1
        if self.parent is not None:
            self.parent.note_tool_call(name, is_network=network)

    def note_usage(self, usage: Usage | None) -> None:
        if usage is None:
            return
        if usage.input_tokens:
            self.input_tokens += usage.input_tokens
        if usage.output_tokens:
            self.output_tokens += usage.output_tokens
        if self.parent is not None:
            self.parent.note_usage(usage)

    def note_subagent(self, depth: int = 1) -> None:
        self.subagent_calls += 1
        self.max_recursion_depth = max(self.max_recursion_depth, depth)
        if self.parent is not None:
            self.parent.note_subagent(depth + 1)

    def allows_tool(self, name: str, *, is_network: bool | None = None) -> bool:
        """Per-dimension gate before executing a tool (model-call gating
        happens in the loop itself, not here).

        is_network overrides the name-prefix heuristic with ground truth
        from tool classification; the loop always passes it.
        """
        if self.limits.max_tool_calls is not None and self.tool_calls >= self.limits.max_tool_calls:
            return False
        network = is_network if is_network is not None else self._net(name)
        if network:
            return (
                self.limits.max_network_calls is None
                or self.network_calls < self.limits.max_network_calls
            )
        return True

    # -- state ---------------------------------------------------------------

    def elapsed_s(self) -> float:
        return max(0.0, self.clock.now() - self.started_at)

    def estimated_cost(self) -> float | None:
        # None = pricing unknown: never invent a number.
        if self.limits.input_price_per_mtok is None and self.limits.output_price_per_mtok is None:
            return None
        cost = 0.0
        if self.limits.input_price_per_mtok is not None:
            cost += (self.input_tokens / 1_000_000) * self.limits.input_price_per_mtok
        if self.limits.output_price_per_mtok is not None:
            cost += (self.output_tokens / 1_000_000) * self.limits.output_price_per_mtok
        return cost

    def exhausted(self) -> tuple[str, ...]:
        hits: list[str] = []
        # Count dimensions report at the ceiling (>=): the gates block new
        # work exactly there, so the snapshot must agree. Wall-time and cost
        # stay strict (>) — no exact-boundary gate exists for them.
        if (
            self.limits.max_model_calls is not None
            and self.model_calls >= self.limits.max_model_calls
        ):
            hits.append(MODEL_CALLS)
        if self.limits.max_tool_calls is not None and self.tool_calls >= self.limits.max_tool_calls:
            hits.append(TOOL_CALLS)
        if (
            self.limits.max_network_calls is not None
            and self.network_calls >= self.limits.max_network_calls
        ):
            hits.append(NETWORK_CALLS)
        if (
            self.limits.max_subagent_calls is not None
            and self.subagent_calls >= self.limits.max_subagent_calls
        ):
            hits.append(SUBAGENTS)
        if (
            self.limits.max_recursion_depth is not None
            and self.max_recursion_depth >= self.limits.max_recursion_depth
        ):
            hits.append(RECURSION_DEPTH)
        cost = self.estimated_cost()
        if self.limits.max_cost is not None and cost is not None and cost > self.limits.max_cost:
            hits.append(COST)
        if (
            self.limits.max_wall_time_s is not None
            and self.elapsed_s() > self.limits.max_wall_time_s
        ):
            hits.append(WALL_TIME)
        ordered = [name for name in PRIORITY if name in hits]
        return tuple(ordered)

    def first_exhausted(self, *, ignore: tuple[str, ...] = ()) -> str | None:
        """First exhausted dimension, optionally skipping some.

        The loop ignores tool/network exhaustion mid-turn: per-call gating
        already blocks execution and the model must still observe the
        rejection and answer. Termination then comes from the model-call
        ceiling, loop detection, or a final answer.
        """
        hits = [name for name in self.exhausted() if name not in ignore]
        return hits[0] if hits else None

    def snapshot(self) -> dict[str, Any]:
        return {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "network_calls": self.network_calls,
            "subagent_calls": self.subagent_calls,
            "max_recursion_depth": self.max_recursion_depth,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "wall_time_s": round(self.elapsed_s(), 1),
            "estimated_cost": self.estimated_cost(),
            "exhausted": list(self.exhausted()),
        }


@dataclass(frozen=True, slots=True)
class EmergencyCircuitBreaker:
    """Hard safety cutoff layered over an observable usage meter.

    ``BudgetMeter`` records work.  This class is the authority that decides
    whether another model/tool operation may begin, keeping emergency limits
    separate from progress-based turn governance.
    """

    meter: BudgetMeter

    def before_model_call(self) -> str | None:
        return self.meter.first_exhausted(ignore=(TOOL_CALLS, NETWORK_CALLS))

    def allows_tool(self, name: str, *, is_network: bool | None = None) -> bool:
        return self.meter.allows_tool(name, is_network=is_network)

    def snapshot(self) -> dict[str, Any]:
        return {
            "tripped": self.before_model_call() is not None,
            "reason": self.before_model_call(),
            "usage": self.meter.snapshot(),
        }


__all__ = [
    "COST",
    "DEFAULT_NETWORK_NAMESPACES",
    "MODEL_CALLS",
    "NETWORK_CALLS",
    "PRIORITY",
    "RECURSION_DEPTH",
    "SUBAGENTS",
    "TOOL_CALLS",
    "WALL_TIME",
    "BudgetMeter",
    "EmergencyCircuitBreaker",
    "TurnBudgetLimits",
]
