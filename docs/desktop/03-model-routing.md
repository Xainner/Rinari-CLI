# 03 — Model routing protocol

> Status (2026-09-09): both P2 items implemented. Per-agent effort is
> stored on the assignment, validated at config time and reaches the
> invocation through the existing `reasoning_effort` path (pinned
> end-to-end in `test_agents_runtime.py`; also fixed `spec.agent` →
> `definition.name`, which had silently disabled the model override).
> `model.capabilities` returns the normalized matrix (pinned in
> `test_engine_agents.py`).

## What Code needs

The desktop already assigns model/fallback/enabled per built-in agent
(`agent.config.get/set`, working). Two gaps remain: per-agent reasoning
effort has no plumbing (Code deliberately hides it rather than store a
decorative value), and there is no capability source to validate an
assignment before use.

## Current engine state

- Agent invocation accepts effort: `runtime/agent.py` takes
  `reasoning_effort` and threads it into the call (line ~538). But agent
  assignment (`agent_configs` service, surfaced via `agent.config.get/set`)
  carries only model/fallback/enabled — effort cannot be stored per
  agent, so the desktop cannot offer it honestly.
- `ModelRecord` carries `capabilities` (`dict | None`) and
  `availability`; the desktop sees them per model but has no matrix view
  and no "required for this role" signal.

## Proposal A — per-agent effort (P2)

Extend the agent assignment (service + `agent.config.set` params +
`agent_list` view) with:

```json
{ "agent": "explorer", "effort": "low | medium | high | null" }
```

`null` means inherit the session/turn effort. The agent runner passes
the resolved effort into the existing `reasoning_effort` call path —
no new routing architecture, just stored config reaching an existing
parameter. Validation: unknown effort strings are `INVALID_PARAMS`,
never silently coerced.

## Proposal B — capability matrix (P2)

```text
model.capabilities { provider, provider_model_id } → full capability map
```

Code needs, per model: tools (bool), vision, reasoning, streaming,
max context window where known. Proposal: normalize the capability map
keys engine-wide (document the key set in `tools.md` or the provider
spec), and add one derived field — never computed desktop-side:

```json
{ "supports_tools": true, "unknown": ["vision"] }
```

`unknown` lists capabilities the engine could not determine, so the UI
renders "unknown" instead of guessing `false`. Assignment validation
stays advisory: the desktop warns ("this model has no tool support and
cannot serve Main Agent in BUILD") but the engine remains the enforcer
at invocation time.

## Acceptance criteria

- [ ] Setting effort on an agent demonstrably changes the effort of its
      next invocation (observable in trace/usage, not just stored).
- [ ] Unset effort inherits session effort (no behavior change by default).
- [ ] Capability gaps render as "unknown", never as invented `false`.
- [ ] Engine rejects (not coerces) invalid effort values with a machine code.

## Non-goals

- No automatic model routing in v1 (explicit assignment only).
- No cost computation desktop-side (engine usage truth only).
- No new routing architecture — config reaching existing parameters.
