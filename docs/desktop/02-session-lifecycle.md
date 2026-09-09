# 02 — Session lifecycle protocol

## What Code needs

The desktop must offer close/archive/delete and, later, branching —
without inventing persistence semantics. It also needs two contract pins
on surfaces Code already consumes.

## Current engine state

- `SessionRecord.state` (`src/rinari/storage/records.py`) only ever holds
  `"active"` (`SESSION_STATE_ACTIVE` in
  `src/rinari/application/session_service.py`). No close/archive/delete.
- `SessionService.fork(ref, title)` exists (new identity, copied
  conversation context) but is **not** exposed in the engine protocol.
- `session.history` (`server.py::_session_history`) returns a
  repo-ordered window with `total`/`has_more`; rows serialize via
  `message_to_dict` (`snapshots.py`): id/seq/role/content/tool_calls/
  tool_call_id/name/created_at.
- `session.events` supports `after_seq` pagination with the same envelope
  shape (id/seq/type/payload/created_at).

## Proposal A — lifecycle (P1)

New states: `closed` (hidden from lists, restorable), `archived`
(read-only, kept), `deleted` (removed per cascade rules below).

```text
session.close    { ref } → marks closed; active turns must finish/cancel first
session.archive  { ref } → marks archived; rejects new turns
session.delete   { ref, cascade: bool } → removes record
```

Cascade semantics (engine-owned, explicit):

```text
tasks        → deleted with the session, listed in the response
checkpoints  → kept (addressable by id) unless cascade: true
artifacts    → kept (store is content-addressed) unless cascade: true
context      → deleted with the session
queue        → drained; caller is told how many were dropped
active turn  → delete/close is rejected with TURN_RUNNING unless the
               caller cancels first (no silent kill)
```

Response echoes what happened:

```json
{ "ok": true, "result": { "session": { "id": "...", "state": "closed" } } }
{ "ok": true, "result": { "deleted": { "id": "..." }, "cascade": { "tasks": 4, "checkpoints_kept": 2 } } }
```

## Proposal B — branching (P2)

`session.branch { ref, title?, checkpoint_id? }` built on the existing
`SessionService.fork`, extended to fork task graph + compact state +
checkpoints, not just conversation. Response returns the new session plus
`branched_from: { session_id, event_seq }` ancestry. Copying visible chat
messages without task/context state is explicitly out.

## Proposal C — contract pins (P0, no engine code expected)

These pin what Code already depends on after its stabilization cycle:

1. **`session.history` ordering**: rows ascending by `seq`; `created_at`
   is the persisted timestamp (ISO-8601). Code renders history and live
   turns through the same path and treats any reordering as a bug.
2. **Row shape stability**: `message_to_dict` fields are additive-only.
   Tool calls stay in `tool_calls`, never folded into `content`.
3. **`session.events` as activity source**: historical activity panels
   will page through `session.events` (`after_seq`, `limit` 1..500).
   Event `type` values for turn/tool/approval lifecycle are stable
   strings; new types are additive.

## Acceptance criteria

- [ ] Closing a session with a running turn fails with a machine code
      (no silent kill, no invented terminal state).
- [ ] Delete response accounts for every owned object (kept or removed).
- [ ] History reload renders identically to the live thread (order +
      timestamps preserved).
- [ ] `session.branch`, when built, preserves tasks/context/checkpoints
      (verified by reopening the branch after restart).

## Non-goals

- No desktop-invented lifecycle states.
- No "duplicate chat as new session" fake branching.
- No retention/GDPR policy in v1 (explicit delete is enough).
