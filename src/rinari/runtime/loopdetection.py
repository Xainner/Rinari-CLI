"""Loop detection (phase 4): spot degenerate model behavior, force a change.

Detectors (harness.md loop-detection list):

    same tool/args          the same (tool, canonical args) repeated within the
                            recent action window
    two-action oscillation  A,B,A,B in the last four actions
    repeated rewrites       a file brought back to a state it already had: the
                            same edit applied again, an edit undone, or the
                            same content written again. Different successive
                            edits of one file are progress, not a loop.
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

import hashlib
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


def _digest(*parts: object) -> str:
    raw = json.dumps(parts, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file_changes(name: str, arguments: object) -> list[tuple[str, str, str | None]]:
    """What a write does to each file: ``(path, state, undo)``.

    ``state`` identifies the change itself (the written content, or the edit);
    ``undo`` is the state an earlier change would have if this one reverses
    it. A write has no undo: writing earlier content again is already seen as
    the same state.
    """
    if name not in _DEFAULT_REWRITES or not isinstance(arguments, dict):
        return []
    if name == "fs.write":
        path = arguments.get("path")
        if not path:
            return []
        return [(str(path), _digest("write", arguments.get("content")), None)]
    rows = arguments.get("files")
    if not isinstance(rows, list):
        rows = [{"path": arguments.get("path"), "edits": [arguments]}]
    changes: list[tuple[str, str, str | None]] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("path"):
            continue
        edits = row.get("edits")
        for edit in edits if isinstance(edits, list) else ():
            if not isinstance(edit, dict):
                continue
            old, new = edit.get("old_string"), edit.get("new_string")
            changes.append((str(row["path"]), _digest("edit", old, new), _digest("edit", new, old)))
    return changes


def _error_signature(code: str, message: str) -> str:
    # Message prefix only: volatile suffixes (paths, ids) should not defeat
    # detection, but full-message hashing would over-trigger on detail noise.
    return f"{code}:{message[:80]}"


class LoopDetector:
    def __init__(self, *, repeats: int = 3, window: int = 12) -> None:
        self._repeats = max(2, repeats)
        self._window = max(4, window)
        self._actions: list[str] = []  # canonical tool and arguments
        self._errors: list[tuple[str, str]] = []  # (signature, tool)
        self._denials: dict[str, int] = {}
        # Per file: the states its writes produced, and how many writes brought
        # it back to one of them.
        self._file_states: dict[str, set[str]] = {}
        self._rewrites: dict[tuple[str, str], int] = {}
        self._last_rewrite: tuple[str, str] | None = None
        self._subagents: list[str] = []
        # Identity -> the model response whose calls earned the nudge. A stop
        # needs the model to have read it, so never in that same response.
        # Without begin_response() (direct callers) every check stands alone.
        self._nudged: dict[tuple[str, object], int | None] = {}
        self._response: int | None = None
        self._action_serial = 0
        self._error_serial = 0
        self._subagent_serial = 0
        self._reported: dict[tuple[str, object], object] = {}

    # -- recorders -----------------------------------------------------------

    def begin_response(self) -> None:
        """The calls that follow come from a new model response."""
        self._response = (self._response or 0) + 1

    def record_tool(self, name: str, arguments: object) -> None:
        self._action_serial += 1
        key = f"{name}|{_canonical_args(arguments)}"
        self._actions.append(key)
        del self._actions[: -self._window]
        self._last_rewrite = None
        for path, state, undo in _file_changes(name, arguments):
            seen = self._file_states.setdefault(path, set())
            if state in seen or (undo is not None and undo in seen):
                key = (name, path)
                self._rewrites[key] = self._rewrites.get(key, 0) + 1
                self._last_rewrite = key
            seen.add(state)

    def record_error(self, name: str, code: str, message: str) -> None:
        self._error_serial += 1
        signature = _error_signature(code, message)
        self._errors.append((signature, name))
        del self._errors[: -self._window]
        if code == "APPROVAL_DENIED":
            self._denials[name] = self._denials.get(name, 0) + 1

    def record_subagent(self, objective: str) -> None:
        self._subagent_serial += 1
        self._subagents.append(objective)
        del self._subagents[: -self._window]

    # -- detection ------------------------------------------------------------

    def check(self) -> LoopSignal | None:
        for kind in _PRIORITY:
            detail = _DETECTORS[kind](self)
            if detail is None:
                continue
            # An unchanged historical signal is not a second occurrence.
            evidence = (
                (detail, self._error_serial)
                if kind in (KIND_SAME_ERROR,)
                else (detail, self._action_serial)
                if kind in (KIND_SAME_TOOL, KIND_OSCILLATION)
                else (detail, self._subagent_serial)
                if kind == KIND_SUBAGENT
                else detail
            )
            identity = (
                kind,
                self._last_rewrite
                if kind == KIND_REWRITE
                else self._actions[-1]
                if kind == KIND_SAME_TOOL
                else self._errors[-1]
                if kind == KIND_SAME_ERROR
                else self._subagents[-1]
                if kind == KIND_SUBAGENT
                else kind,
            )
            if self._reported.get(identity) == evidence:
                continue
            nudged = identity in self._nudged
            nudged_in = self._nudged.get(identity)
            if nudged and self._response is not None and nudged_in == self._response:
                # Repeated inside the response that was just nudged (a model
                # can emit four identical calls at once): the nudge has not
                # reached it yet, so this is not a second offence.
                continue
            if (
                not nudged
                and self._response is not None
                and self._response in self._nudged.values()
            ):
                # One nudge per response: the same repetition also trips
                # same-error, and one note covers both.
                self._reported[identity] = evidence
                self._nudged[identity] = self._response
                continue
            self._reported[identity] = evidence
            action = STOP if nudged else NUDGE
            if action == NUDGE:
                self._nudged[identity] = self._response
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
    # One return to an earlier state can be a deliberate revert; the second is
    # going in circles.
    if det._last_rewrite is not None:
        name, path = det._last_rewrite
        count = det._rewrites[det._last_rewrite]
        if count >= det._repeats - 1:
            return f"{path} returned to an earlier state by {name} {count} times"
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
