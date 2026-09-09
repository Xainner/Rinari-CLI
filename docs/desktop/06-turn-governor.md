# 06 — Turn governor protocol

## Context

P1/P2 from `docs/desktop/01–05` landed on this branch (project surface,
session close/delete, artifact export, effort, capabilities, soul,
intelligence, branch, PTY). This document specifies the remaining
protocol surface for the automatic turn governor
(`fixes/RINARI_AUTOMATIC_TURN_GOVERNOR.md` in Rinari-Code, phases 3–5):
the stop event, progress events, and the snapshot usage block the
desktop already consumes.

## Desktop state (done, Rinari-Code)

Code implemented its side without waiting for emission:

- `turn.stopped` maps to a terminal `stopped` execution with
  `stopReason { code, message, modelCalls?, toolCalls?, wallTimeS? }`
  (`src/features/engine/turnRuntime.ts`). Tools keep their last-known
  state — the desktop never invents their outcome.
- Emergency card (reason + usage + Continue / Review activity);
  Continue pre-fills a composer draft for a **new** turn in the same
  session, never resurrecting the stopped meter
  (`src/components/chat/TurnExecutionBlock.tsx`, `ChatView.tsx`).
- `turn.stopped` triggers session refresh; reducer/mapper/contract
  tests cover it (`turnRuntime.test.ts`, 21 frontend tests green).
- No budget picker exists in the composer; usage panels are
  information-only (no countdowns). Both governor DoD items already hold.

Until the engine emits, that UI is wired and tested but inactive.

## Current engine state

- No `turn.stopped` emission in `engine_protocol/turns.py`.
- No `governor.*` or `usage.updated` events.
- Snapshot `active_turns[]` carry status/activities but no aggregated
  `runtime` usage block.
- Cancellation is authoritative and immediate (keep it that way).

## Proposal

### `turn.stopped` (P0 for governor rollout)

Terminal event, distinct from `turn.completed` and `turn.failed`:

```json
{
  "type": "event",
  "event": "turn.stopped",
  "payload": {
    "turn_id": "t_...",
    "session_id": "ses_...",
    "reason": { "code": "wall_time | model_calls | loop | context | emergency", "message": "..." },
    "usage": { "model_calls": 500, "tool_calls": 1832, "wall_time_s": 6120 }
  }
}
```

Semantics:

```text
- emitted at-most-once per turn, at-least-once per stop (no silent ends);
- never follows engine activity for the same turn id afterwards;
- never replaces turn.completed for genuinely finished work;
- cancellation still wins: a cancelled turn emits turn.cancelled, not stopped;
- unknown usage keys are additive; missing usage renders as "unknown", never zero.
```

### Governor progress events (P1)

Mechanics only, never chain-of-thought:

```text
governor.progress    { turn_id, session_id, status, novelty_score?, stagnant_cycles? }
governor.nudge       { turn_id, session_id, signal }
governor.compact     { turn_id, session_id, status: started|completed }
governor.consolidate { turn_id, session_id }
governor.stop        { turn_id, session_id, reason }  (advisory; turn.stopped is terminal)
usage.updated        { session_id, turn_id?, model_calls, tool_calls, tokens? }
```

The desktop renders these as activity rows; high-frequency progress
events may be coalesced engine-side as long as ordering per turn holds.

### Snapshot `runtime` block (P0)

```json
{ "active_turns": [{ "turn_id": "...", "runtime": {
  "progress": "healthy | stagnating | consolidating",
  "model_calls": 41, "tool_calls": 120, "subagents": 3, "wall_time_s": 402
}}]}
```

Reconstruction must not require replaying historical governor events.

### Continue semantics (no engine change)

Continue is a normal new `session.turn.start` in the same session.
History, tasks, compact state, artifacts and verification persist by
construction. The engine must simply accept a new turn after a stop —
no resurrection path, no special method.

## Acceptance criteria

- [ ] Every governor stop emits `turn.stopped` with a machine `code`
      (no silent ends; no `completed` for stopped work).
- [ ] Cancelled turns still emit `turn.cancelled`, never `stopped`.
- [ ] `usage.updated` (or equivalent) keeps long-turn progress visible
      without polling `usage.get`.
- [ ] Post-restart snapshot rebuilds the desktop without event replay.
- [ ] Protocol tests: stop taxonomy, cancel-wins, restart-during-stop,
      snapshot-restore, Code reconnect mid-turn.

## Non-goals

- No chain-of-thought exposure (explicit governor non-goal).
- No automatic infinite retry; no resurrection of stopped meters.
- No desktop-side budget/limit invention — counts stay informational.
- No cost computation in the desktop (engine usage truth only).
