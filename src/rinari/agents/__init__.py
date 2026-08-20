"""Multi-agent runtime (phase 6): definitions, registry, worktrees, orchestrator."""

from rinari.agents.definition import (
    BUILTIN_AGENTS,
    MAX_CONCURRENT,
    MAX_DEPTH,
    MAX_TOTAL,
    AgentBudget,
    AgentDefinition,
    builtin_agents,
)
from rinari.agents.orchestrator import AgentOrchestrator, AgentResult, OrchestratorError
from rinari.agents.registry import AgentRegistry

__all__ = [
    "BUILTIN_AGENTS",
    "MAX_CONCURRENT",
    "MAX_DEPTH",
    "MAX_TOTAL",
    "AgentBudget",
    "AgentDefinition",
    "AgentOrchestrator",
    "AgentRegistry",
    "AgentResult",
    "OrchestratorError",
    "builtin_agents",
]
