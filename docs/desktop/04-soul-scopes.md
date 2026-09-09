# 04 — Soul scopes protocol

> Status (2026-09-09): session scope implemented. `sessions.soul_id`
> (migration 0021, NULL = inherit) validated against the SoulStore at
> write time; `soul.get_effective` resolves session pin → global Soul 3.0
> chain (active custom → legacy `~/soul.md` → bundled default) with an
> explicit `source`; a pin pointing at a removed Soul fails NOT_FOUND
> instead of silently switching personality. Prompt composition honors
> the pin (`build_assembler_context`); subagents stay functional
> (`soul=""`, pinned). Project-level default deferred: no project config
> surface exists yet, and inventing one here would bypass trust.

## What Code needs

Soul is a real domain object globally (list/get/create/update/remove/
activate, working from both CLI and desktop). The product hierarchy needs
project and session overrides with deterministic resolution — currently
impossible, and the desktop must not fake it with prompt concatenation.

## Current engine state

- `SoulStore` (`src/rinari/soul/store.py`) is global-only: bundled +
  `~/.rinari/souls/` + an `active_soul` pointer file. Its header states
  project/session scopes are out of scope until the engine owns them.
- `soul_id` validation exists (`validate_soul_id`); no `soul_id` column
  on sessions; no project-level soul binding or trust interaction.
- Invariant (both repos): Soul controls voice/identity only. It never
  overrides tool permissions, approvals, verification truth, or safety
  boundaries — those stay code-enforced outside Soul.

## Proposal (P2)

### Storage

- Sessions gain `soul_id TEXT NULL` (migration; `NULL` = inherit).
- Project override lives in trusted project configuration only, subject
  to the existing project-trust decision (`src/rinari/trust/`).
  Untrusted checkouts never inject personality.

### Resolution (engine-owned, deterministic)

```text
bundled default
  → global active_soul
    → project override (trusted projects only)
      → session soul_id
```

### Protocol

```text
soul.get_effective { ref: <session ref> }
  → { soul_id, source: bundled|global|project|session }
```

```text
session.soul.set   { ref, soul_id | null }   (null = inherit)
session.soul.clear { ref }
```

Project override management reuses the project config surface when it
lands (see `01-project-workspace.md`); no separate soul-project CRUD.

### Validation

Unknown `soul_id` → `NOT_FOUND` machine code at set time, never silent
fallback to default (the UI must show the failure, not a wrong
personality). Effective-soul changes emit an event so CLI and desktop
stay consistent.

## Acceptance criteria

- [ ] Precedence is exactly the documented order in every client.
- [ ] Untrusted project config cannot change personality (tested).
- [ ] Session override survives restart; clearing restores inheritance.
- [ ] Subagents keep functional role personas by default; only the main
      agent receives the active Soul (engine prompt composition).
- [ ] No Soul content can alter permissions, approvals, verification
      display, or safety behavior (regression-tested boundary).

## Non-goals

- No personality sliders in v1 (they must compile into a structured
  Soul definition — see Code's debt log — not prompt fragments).
- No Soul marketplace/community format in v1.
- No per-message soul switching.
