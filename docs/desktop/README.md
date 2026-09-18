# Desktop protocol needs (Rinari Code → Engine)

Proposals from the Rinari Code stabilization cycle (2026-09-09) for work
that must be engine-owned before the desktop can build on it. Each
document follows the same shape: what Code needs, current engine state,
proposed contract, acceptance criteria, non-goals.

Ownership rule (both repos): the engine owns sessions, projects,
providers, tools, policy, tasks, verification, artifacts, context and
persistent state. The desktop never reimplements them; it consumes this
protocol.

## Documents

1. `01-project-workspace.md` — recent projects, open, status, intelligence.
2. `02-session-lifecycle.md` — close/archive/delete + cascade, branching,
   history contract.
3. `03-model-routing.md` — per-agent reasoning effort, capability matrix.
4. `04-soul-scopes.md` — project/session Soul overrides.
5. `05-runtime-surfaces.md` — PTY registry, artifact export, snapshot and
   cancellation contracts.
6. `06-turn-governor.md` — stop/progress events and snapshot usage block.
7. `06-interactive-workspace.md` — questions and session workspaces
   (`interactive_questions_v1`, `desktop_workspace_v1`).
8. `07-web-previews.md` — static/dev previews owned by the engine.
9. `09-peer-messaging.md` — messages between the agent sessions of a board
10. `10-project-flow.md` — stages of a project or chat derived from persisted turns (`flow.get`)
   (`session_peer_messaging_v1`): groups, consent, provenance ceiling.

## Priority (Code's view)

```text
P0 contract pins (no engine code, mostly guarantees):
  session.history ordering/timestamps, snapshot shape, cancel terminality

P1 new engine surface:
  session.close/delete, project.list_recent/open/status, artifact.export

P2 later:
  project intelligence, session.branch, per-agent effort, soul scopes, PTY
```

## Conventions for every proposal

- Method names use the existing dotted namespace (`domain.action`).
- Every request has an ID; every response references it (already true).
- Machine codes drive behavior, never localized strings.
- Additive fields are forward-compatible; breaking changes bump
  `protocol.PROTOCOL_VERSION`.
- Desktop fails clearly on missing capabilities, never silently degrades
  core turn execution.
