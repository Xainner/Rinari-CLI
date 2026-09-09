"""Progress-aware controller for automatic Rinari turns."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum

from rinari.runtime.progress import ProgressKind, ProgressMonitor, ProgressObservation


class GovernorAction(StrEnum):
    CONTINUE = "continue"
    CONSOLIDATE = "consolidate"
    NUDGE = "nudge"
    FINALIZE = "finalize"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class GovernorDecision:
    action: GovernorAction
    progress: ProgressKind
    stagnant_cycles: int
    recovery_attempts: int
    reason: str | None = None


class TurnGovernor:
    def __init__(self, *, max_recovery_attempts: int = 3) -> None:
        self.progress = ProgressMonitor()
        self.max_recovery_attempts = max(1, max_recovery_attempts)
        self.recovery_attempts = 0
        self.last: ProgressObservation | None = None

    def after_tool(self, name: str, arguments: object, result: object, *, ok: bool) -> None:
        self.progress.observe_tool(name, arguments, result, ok=ok)

    def after_cycle(self, *, looping: bool = False) -> GovernorDecision:
        observation = self.progress.finish_cycle(looping=looping)
        self.last = observation
        if observation.kind in (ProgressKind.HEALTHY, ProgressKind.SLOW):
            return self._decision(GovernorAction.CONTINUE, observation)
        self.recovery_attempts += 1
        if looping and self.recovery_attempts >= self.max_recovery_attempts:
            return self._decision(GovernorAction.STOP, observation, "persistent_loop")
        action = {
            1: GovernorAction.CONSOLIDATE,
            2: GovernorAction.NUDGE,
            3: GovernorAction.FINALIZE,
        }.get(self.recovery_attempts, GovernorAction.STOP)
        return self._decision(
            action,
            observation,
            "stagnation" if action is GovernorAction.STOP else None,
        )

    def snapshot(self) -> dict[str, object]:
        observation = self.last
        return {
            "execution": "automatic",
            "recovery_attempts": self.recovery_attempts,
            "max_recovery_attempts": self.max_recovery_attempts,
            "progress": asdict(observation) if observation is not None else None,
        }

    def _decision(
        self,
        action: GovernorAction,
        observation: ProgressObservation,
        reason: str | None = None,
    ) -> GovernorDecision:
        return GovernorDecision(
            action=action,
            progress=observation.kind,
            stagnant_cycles=observation.stagnant_cycles,
            recovery_attempts=self.recovery_attempts,
            reason=reason,
        )


RECOVERY_PROMPTS = {
    GovernorAction.CONSOLIDATE: (
        "[runtime governor] Consolidate the evidence already gathered. Identify what is known, "
        "what remains, and avoid repeating identical actions."
    ),
    GovernorAction.NUDGE: (
        "[runtime governor] The current strategy is not producing new evidence. Change approach, "
        "arguments, or tool; do not retry the same failed action."
    ),
    GovernorAction.FINALIZE: (
        "[runtime governor] Recovery is exhausted. Stop calling tools and provide the most useful "
        "truthful answer possible, clearly naming any unresolved blocker."
    ),
}


__all__ = ["RECOVERY_PROMPTS", "GovernorAction", "GovernorDecision", "TurnGovernor"]
