# 05 — Runtime surfaces protocol

## What Code needs

Terminal, artifact export, and two contract pins on runtime behavior the
desktop already depends on after its stabilization cycle.

## Current engine state

- PTY execution exists but is tool-call-scoped: `PtyRegistry` /
  `PtyHandle` in `src/rinari/tools/native/ptytools.py`, resolved per
  `ToolContext` (`pty_start/read/write/resize/terminate` are agent tools,
  not engine sessions). Nothing streams PTY output as protocol events.
- `ArtifactStore.export(uri, dest)` exists (`src/rinari/artifacts/store.py`)
  but is not exposed in the engine protocol (Code previews via
  `artifact.read` only).
- Snapshot: `build_snapshot` (`engine_protocol/snapshots.py`) plus
  `active_turns` / `pending_approvals` (`turns.py:149`). Code reconciles
  cancellation and restart against exactly this shape.
- Cancellation: the engine emits terminal `turn.cancelled`; Code treats a
  missing terminal as "reconcile via snapshot", never as proof.

## Proposal A — PTY sessions (P2)

> Status (2026-09-09): implemented as `EnginePtyService`
> (`src/rinari/engine_protocol/pty.py`, one per engine process) over the
> same `PtyRegistry` backend the tool layer uses — no second
> implementation. `pty.start/write/resize/read/list/terminate` plus
> `pty.output`/`pty.exit` events on the shared outbox (desktop renders
> xterm from events; `pty.read` covers UI-reload recovery). Param
> validation is platform-independent; only the spawn needs POSIX
> (`PTY_UNSUPPORTED` elsewhere, never a fake shell). cwd must be a real
> directory and never the home root (`PERMISSION_DENIED`, same rule as
> projects); unknown handles are `NOT_FOUND` on every platform;
> terminate on a dead handle is no-op success; restart reports none.
> Pinned in `tests/unit/test_engine_pty.py` (spawn/output/exit flows are
> POSIX-only; validation, NOT_FOUND and PTY_UNSUPPORTED run everywhere).

Promote the existing `PtyRegistry` mechanics to an engine-owned session
registry (one per engine process, keyed by handle id), still behind
policy/sandbox/approvals — no second PTY backend in the desktop:

```text
pty.start     { command, cwd?, env?, cols?, rows? } → { pty_id }
pty.write     { pty_id, data }
pty.resize    { pty_id, cols, rows }
pty.terminate { pty_id }
```

Events (the desktop renders xterm from these, never polls in a loop):

```text
pty.output { pty_id, session_id?, data }
pty.exit   { pty_id, exit_code }
```

Lifecycle: handles die with their process; `pty.terminate` on a dead
handle is a no-op success; restart of the engine drops all handles and
the snapshot reports none (desktop shows "terminal ended", never a
frozen ghost). Unsupported platforms report a machine code
(`PTY_UNSUPPORTED`), never a fake shell.

## Proposal B — artifact export (P1)

```text
artifact.export { uri, dest_dir } → { path }
```

The engine validates the destination (inside an allowed root, no
traversal), writes the bytes, and returns the real path. The desktop
opens a native destination dialog and never guesses store paths. This
unblocks Code's artifact gallery "save" action with zero new trust
surface: writes stay engine-side.

## Proposal C — contract pins (P0, guarantees, not new code)

1. **Snapshot shape**: `runtime.snapshot.get` keeps `active_turns[]`
   with `turn_id`, `session_id`, `status`, `started_at` (epoch seconds),
   `preparation_stage?`, `activities[]` (`event`, `tool_call_id`,
   `tool`, `arguments?`, `duration_ms?`), and `pending_approvals[]`
   with `approval_id`, `session_id`, `turn_id`, `capability`, `target`,
   `risk`, `description`. Additive-only.
2. **Cancel terminality**: every accepted turn ends in exactly one of
   `turn.completed` / `turn.cancelled` / `turn.failed`, delivered
   at-least-once per turn. If delivery is ever impossible (crash), the
   post-restart snapshot contains no trace of the turn, which the
   desktop treats as "reconcile, don't invent".
3. **Busy truth**: while the engine owns an active turn for a session,
   the snapshot reports it — the desktop blocks conflicting turns on
   exactly this signal.

## Acceptance criteria

- [ ] PTY output streams to the desktop without polling; exit always
      arrives; dead handles never freeze the UI.
- [ ] `artifact.export` rejects traversal outside the allowed root.
- [ ] Snapshot shape changes are additive; removals bump protocol major.
- [ ] A cancelled turn is never followed by engine activity for the
      same turn id.

## Non-goals

- No interactive browser view in v1 (activity panel only, separate track).
- No desktop-side PTY, shell bridge, or artifact path inference.
- No terminal session persistence across engine restarts.
