"""Observable progress signals for long automatic turns."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

_EVIDENCE = re.compile(
    r"(?:artifact://\S+|https?://\S+|(?:[A-Za-z]:)?[/\\][\w./\\-]+|\b[0-9a-f]{7,40}\b)"
)


class ProgressKind(StrEnum):
    HEALTHY = "healthy"
    SLOW = "slow"
    STAGNANT = "stagnant"
    LOOPING = "looping"


@dataclass(frozen=True, slots=True)
class ProgressObservation:
    kind: ProgressKind
    novelty_score: float
    new_evidence: int
    repeated_failures: int
    stagnant_cycles: int


@dataclass(slots=True)
class ProgressMonitor:
    """Track stable novelty without inspecting private model reasoning."""

    _fingerprints: set[str] = field(default_factory=set)
    _evidence: set[str] = field(default_factory=set)
    _cycle_progress: int = 0
    _cycle_failures: int = 0
    _repeated_failures: int = 0
    _stagnant_cycles: int = 0

    def observe_tool(self, name: str, arguments: Any, result: Any, *, ok: bool) -> None:
        text = result if isinstance(result, str) else repr(result)
        wire = json.dumps(
            {"tool": name, "arguments": arguments, "result": text[:4000]},
            sort_keys=True,
            default=str,
            ensure_ascii=False,
        )
        fingerprint = hashlib.sha256(wire.encode("utf-8")).hexdigest()
        evidence = set(_EVIDENCE.findall(text))
        new_evidence = evidence - self._evidence
        novel = fingerprint not in self._fingerprints or bool(new_evidence)
        self._fingerprints.add(fingerprint)
        self._evidence.update(new_evidence)
        if novel and ok:
            self._cycle_progress += 1 + len(new_evidence)
            self._repeated_failures = 0
        elif not ok:
            self._cycle_failures += 1
            self._repeated_failures += 1

    def finish_cycle(self, *, looping: bool = False) -> ProgressObservation:
        if looping:
            kind = ProgressKind.LOOPING
        elif self._cycle_progress > 0:
            kind = ProgressKind.HEALTHY
            self._stagnant_cycles = 0
        else:
            self._stagnant_cycles += 1
            kind = ProgressKind.STAGNANT if self._stagnant_cycles >= 2 else ProgressKind.SLOW
        observation = ProgressObservation(
            kind=kind,
            novelty_score=float(self._cycle_progress),
            new_evidence=self._cycle_progress,
            repeated_failures=self._repeated_failures,
            stagnant_cycles=self._stagnant_cycles,
        )
        self._cycle_progress = 0
        self._cycle_failures = 0
        return observation


__all__ = ["ProgressKind", "ProgressMonitor", "ProgressObservation"]
