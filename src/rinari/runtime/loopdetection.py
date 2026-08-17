"""Loop detection (phase 4): spot degenerate model behavior, force a change.

Detectors (harness.md loop-detection list):

    same tool/args          the same (tool, canonical args) repeated within the
                            recent action window
    two-action oscillation  A,B,A,B in the last four actions
    repeated rewrites       the same target path written by fs.write/fs.patch
    same error              the same error signature repeated
    repeated denied approval the same tool denied N times in a row
    duplicated subagent work the same subagent objective repeated (no subagents
                            spawn in phase 4; the recorder is wired so the later
                            multi-agent runtime feeds it for free)
    force strategy change   first hit injects a harness nudge telling the model
                            to change approach; a second hit of the same kind
                            stops the turn (kind="loop")

The detector is per-turn: it never carries state across turns, so the same
legitimate action in successive user turns is not a loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

NUDGE = "nudge"  # keep the turn going, inject a strategy-change instruction
STOP = "stop"  # end the turn with kind="loop"

KIND_SAME_TOOL = "same-tool-args"
KIND_OSCILLATION = "two-action-oscillation"
KIND_REWRITE = "repeated-rewrites"
KIND_SAME_ERROR = "same-error"
KIND_DENIED_APPROVAL = "repeated-denied-approval"
KIND_SUBAGENT = "duplicated-subagent-work"

# Evaluation order: the most decisive (and cheapest) detectors first.
_PRIORITY = (
    KIND_SAME_TOOL,
    KIND_OSCILLATION,
    KIND_REWRITE,
    KIND_DENIED_APPROVAL,
    KIND_SAME_ERROR,
    KIND_SUBAGENT,
)

_DEFAULT_REWRITES = frozenset({"fs.write", "fs.patch"})


@dataclass(frozen=True, slots=True)
class LoopSignal:
    kind: str
    detail: str
    action: str  # NUDGE | STOP


def _canonical_args(arguments: object) -> str:
    try:
        return json.dumps(arguments, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(arguments)


def _canonical_path(name: str, arguments: object) -> str | None:
    if name not in _DEFAULT_REWRITES:
        return None
    if not isinstance(arguments, dict):
        return None
    path = arguments.get("path")
    return str(path) if path else None


def _error_signature(code: str, message: str) -> str:
    # Message prefix only: volatile suffixes (paths, ids) should not defeat
    # detection, but full-message hashing would over-trigger on detail noise.
    return f"{code}:{message[:80]}"


class LoopDetector:
    def __init__(self, *, repeats: int = 3, window: int = 12) -> None:
        self._repeats = max(2, repeats)
        self._window = max(4, window)
        self._actions: list[tuple[str, str]] = []  # (tool, canonical key)
        self._errors: list[tuple[str, str]] = []  # (signature, tool)
        self._denials: dict[str, int] = {}
        self._rewrites: dict[tuple[str, str], int] = {}
        self._subagents: list[str] = []
        self._nudged: set[str] = set()

    # -- recorders -----------------------------------------------------------

    def record_tool(self, name: str, arguments: object) -> None:
        key = f"{name}|{_canonical_args(arguments)}"
        self._actions.append(key)
        del self._actions[: -self._window]
        path = _canonical_path(name, arguments)
        if path is not None:
            self._rewrites[(name, path)] = self._rewrites.get((name, path), 0) + 1

    def record_error(self, name: str, code: str, message: str) -> None:
        signature = _error_signature(code, message)
        self._errors.append((signature, name))
        del self._errors[: -self._window]
        if code == "APPROVAL_DENIED":
            self._denials[name] = self._denials.get(name, 0) + 1

    def record_subagent(self, objective: str) -> None:
        self._subagents.append(objective)
        del self._subagents[: -self._window]

    # -- detection ------------------------------------------------------------

    def check(self) -> LoopSignal | None:
        for kind in _PRIORITY:
            detail = _DETECTORS[kind](self)
            if detail is None:
                continue
            action = STOP if kind in self._nudged else NUDGE
            if action == NUDGE:
                self._nudged.add(kind)
            return LoopSignal(kind=kind, detail=detail, action=action)
        return None

    def nudge_text(self, signal: LoopSignal) -> str:
        return (
            f"[harness loop-detector] {signal.kind}: {signal.detail} "
            "Stop repeating the same approach — change strategy before continuing "
            "(different tool, different arguments, different plan, or state what you "
            "conclude and ask the user)."
        )


def _same_tool(det: LoopDetector) -> str | None:
    tail = det._actions[-det._repeats :]
    if len(tail) < det._repeats:
        return None
    if len(set(tail)) == 1:
        return f"the same tool call repeated {det._repeats} times"
    return None


def _oscillation(det: LoopDetector) -> str | None:
    tail = det._actions[-4:]
    if len(tail) < 4:
        return None
    a, b, c, d = tail
    if a == c and b == d and a != b:
        return "alternating the same two actions (A,B,A,B)"
    return None


def _rewrites(det: LoopDetector) -> str | None:
    for (name, path), count in det._rewrites.items():
        if count >= det._repeats:
            return f"{path} rewritten by {name} {count} times"
    return None


def _denied(det: LoopDetector) -> str | None:
    for name, count in det._denials.items():
        if count >= det._repeats:
            return f"{name} denied {count} times in a row"
    return None


def _same_error(det: LoopDetector) -> str | None:
    tail = det._errors[-det._repeats :]
    if len(tail) < det._repeats:
        return None
    if len(set(tail)) == 1:
        signature, name = tail[0]
        return f"{name} failed with the same error {det._repeats} times ({signature})"
    return None


def _subagent(det: LoopDetector) -> str | None:
    if len(det._subagents) >= det._repeats:
        tail = det._subagents[-det._repeats :]
        if len(set(tail)) == 1:
            return f"the same subagent objective repeated {det._repeats} times"
    return None


_DETECTORS = {
    KIND_SAME_TOOL: _same_tool,
    KIND_OSCILLATION: _oscillation,
    KIND_REWRITE: _rewrites,
    KIND_DENIED_APPROVAL: _denied,
    KIND_SAME_ERROR: _same_error,
    KIND_SUBAGENT: _subagent,
}

__all__ = [
    "KIND_DENIED_APPROVAL",
    "KIND_OSCILLATION",
    "KIND_REWRITE",
    "KIND_SAME_ERROR",
    "KIND_SAME_TOOL",
    "KIND_SUBAGENT",
    "NUDGE",
    "STOP",
    "LoopDetector",
    "LoopSignal",
]
