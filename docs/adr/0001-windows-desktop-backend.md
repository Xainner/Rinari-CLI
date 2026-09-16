# ADR 0001: Windows desktop backend for computer use

Status: PROPOSED (not approved, not implemented)
Date: 2026-09-16
Branch: feat/computer-use

## Context

The validated proposal (rinari-computer-use v0.2) requires a bounded Windows
desktop backend AFTER the browser observation flow is closed (done: immutable
captures, observation binding, input cleanup). The backend must capture an
explicitly authorized window and inject bounded input under a user-issued
grant with target, scopes, visual destination and expiry.

## Candidate APIs (validated public references, not a shipped combination)

- Capture: Windows Graphics Capture, IGraphicsCaptureItemInterop
  CreateForWindow (per-window capture bound to an explicit HWND).
- Semantics: UI Automation / accessibility tree per application (coverage
  not certified in this review; no library selected).
- Input: native input with strict preconditions. SendInput is subject to
  UIPI, does not reset keys held elsewhere, and is NOT per-window isolated
  input. It must not be treated as a scoped primitive without resolving
  that gap first.

## Decisions pending (all required before approval)

1. Exact Rust/Python binding, version, license, feature flags.
2. Windows SDK matrix and packaging/distribution story.
3. Focus/integrity-level story: how the backend proves the authorized
   window is foreground and what happens when it is not.
4. DPI model: physical vs CSS pixels contract per backend call.
5. Privilege stance: NO installer elevation, NO privileged components,
   NO bypassing integrity-level restrictions as a shortcut.

## What this branch implements (engine side, fake backend only)

- rinari/computer: GrantStore (observe/input/send scopes, TTL, revoke),
  GraphicBackend ABC with the input-completion contract, FakeBackend,
  GraphicControlService (grant gate, observation registry, dispatch
  ledger with not_dispatched/dispatched/unknown).
- computer.* tools (state/capture/click/type), grant-gated and
  observation-bound, registered alongside browser tools.
- Policy capabilities computer.observe/computer.operate (read-only deny).
- WindowsBackend exists ONLY as a failing-closed seam.

## Lab gate for real proving

- Separate Windows environment: no personal data, no inherited
  credentials, no broad mounts, externally limited network.
- Backend approval recorded here (binding + version + matrix) BEFORE any
  real capture/input test runs.
- Proving matrix: one fiction app; capture/input/focus/DPI, exclusion of
  non-target surfaces, stop semantics, 20-run batch per flow with zero
  off-target actions (initial filter, per the proposal).
- The personal desktop is NEVER the lab.

## Explicit non-goals

Remote control, unattended operation of real accounts, simultaneous
Linux/macOS desktop backends, automatic privileged installation,
continuous surveillance, sensitive actions without proper intervention.