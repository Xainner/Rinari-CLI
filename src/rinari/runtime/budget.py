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
    # Real coding turns need several inspect/edit/verify cycles. The previous
    # defaults stopped ordinary repository work before it could synthesize.
    max_model_calls: int = 16
    max_tool_calls: int = 64
    max_network_calls: int = 32
    max_wall_time_s: float = 600.0
    # None = no cost limit. Pricing is per-million tokens; None = unknown
    # (then cost is unmeasured and can never exhaust the budget).
    max_cost: float | None = None
    input_price_per_mtok: float | None = None
    output_price_per_mtok: float | None = None
    max_subagent_calls: int = 4
    max_recursion_depth: int = 3


def _default_is_network(name: str) -> bool:
    return name.split(".", 1)[0] in DEFAULT_NETWORK_NAMESPACES


@dataclass(slots=True)
class BudgetMeter:
    limits: TurnBudgetLimits
    clock: Clock
    is_network: Callable[[str], bool] | None = None
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

    @property
    def _net(self) -> Callable[[str], bool]:
        return self.is_network or _default_is_network

    # -- recording -----------------------------------------------------------

    def note_model_call(self) -> None:
        self.model_calls += 1

    def note_tool_call(self, name: str) -> None:
        self.tool_calls += 1
        if self._net(name):
            self.network_calls += 1

    def note_usage(self, usage: Usage | None) -> None:
        if usage is None:
            return
        if usage.input_tokens:
            self.input_tokens += usage.input_tokens
        if usage.output_tokens:
            self.output_tokens += usage.output_tokens

    def note_subagent(self, depth: int = 1) -> None:
        self.subagent_calls += 1
        self.max_recursion_depth = max(self.max_recursion_depth, depth)

    def allows_tool(self, name: str) -> bool:
        """Per-dimension gate before executing a tool (model-call gating
        happens in the loop itself, not here)."""
        if self.tool_calls >= self.limits.max_tool_calls:
            return False
        if self._net(name):
            return self.network_calls < self.limits.max_network_calls
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
        if self.model_calls > self.limits.max_model_calls:
            hits.append(MODEL_CALLS)
        if self.tool_calls > self.limits.max_tool_calls:
            hits.append(TOOL_CALLS)
        if self.network_calls > self.limits.max_network_calls:
            hits.append(NETWORK_CALLS)
        if self.subagent_calls > self.limits.max_subagent_calls:
            hits.append(SUBAGENTS)
        if self.max_recursion_depth > self.limits.max_recursion_depth:
            hits.append(RECURSION_DEPTH)
        cost = self.estimated_cost()
        if self.limits.max_cost is not None and cost is not None and cost > self.limits.max_cost:
            hits.append(COST)
        if self.elapsed_s() > self.limits.max_wall_time_s:
            hits.append(WALL_TIME)
        ordered = [name for name in PRIORITY if name in hits]
        return tuple(ordered)

    def first_exhausted(self) -> str | None:
        hits = self.exhausted()
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
    "TurnBudgetLimits",
]
