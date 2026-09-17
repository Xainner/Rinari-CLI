# Peer messaging between agent sessions (Boards)

`session_peer_messaging_v1` lets the agents of the panels of a Rinari Code
board exchange text messages through the engine. A message is accepted as a
durable delivery, never executed by the sender: the receiver runs its own
turn, under its own permissions, and treats the text as untrusted data from
another agent.

## What Code needs

A board shows N sessions side by side. Each agent must be able to discover
its peers (`session.peers`) and message one of them (`session.send`) without
the user copying text by hand, while the user keeps full control: consent per
destination, toggles per board and per panel, Stop that also stops incoming
deliveries, and a visible trail of every message in both panels.

## Current engine state

Implemented on top of the existing turn scheduler (one active turn per
session, FIFO queue) and the `OperationStore` (`engine-operations.sqlite`,
user_version 3):

- `peer_groups` / `peer_group_members`: atomic membership with `revision`
  and `authorization_epoch`.
- `session_inbox`: durable deliveries with state machine
  `queued → dispatching → running → completed|failed|cancelled|stopped`,
  plus `paused` (Stop, group revoked, authorization changed) and
  `uncertain` (accepted by another engine instance).
- `peer_chain_counters`: deliveries per chain.
- `session_messages.origin_json` (migration 0032): provenance of every
  persisted user-role message.

## Contract

### Methods

| Method | Params | Result |
|---|---|---|
| `session.peer_group.set` | `board_id`, optional `group_id`, `expected_revision` (0 to create), `enabled`, `members[] {session_id, label, send, receive}`, optional `client_id` | `peerGroup` + `warnings[]` |
| `session.peer_group.get` | one of `group_id`, `session_id`, `board_id` | `{group: peerGroup \| null}` |
| `session.peer_group.revoke` | `group_id` | `peerGroup` (disabled, epoch bumped) |
| `session.peer_message.list` | `session_id` | `{messages: peerMessage[]}` |
| `session.peer_message.cancel` | `message_id` | `peerMessage` (only `queued`/`paused` can be cancelled) |
| `session.peer_message.forward` | `target_session_id`, `message`, optional `source_session_id`, `quoted_source` | `peerMessage` with `origin.kind = "user"` |
| `session.queue.resume` | `session_id` | `{resumed}` — lifts the pause set by Stop |
| `session.queue.list` | `session_id` | adds `entries[]` (`queuedPromptEntry`) next to the legacy `queue: string[]` |

Group rules:

- Creation is idempotent per `(board_id, client_id)`: a retry with the same
  composition returns the existing group unchanged; a different composition
  at `expected_revision = 0` is a `CONFLICT` that names the live revision.
- Any other change must carry the current `expected_revision`.
- Widening (member added, flag enabled, group re-enabled) or shrinking the
  membership bumps `authorization_epoch`; consents issued against the previous
  composition stop matching.
- A session belongs to at most one enabled group; adding it elsewhere
  displaces it (the old group's epoch bumps and it emits an update).
- Closing, archiving or deleting a session removes it from its group and
  cancels its pending deliveries.

### Tools (deferred exposure, discoverable via `capability.search`)

| Tool | Capability | Policy |
|---|---|---|
| `session.peers` | `state.read` | allowed; lists only group members with label, project, model, `busy`, `can_send` |
| `session.send` | `session.message` (new) | ASK per destination, `binding_mode = "exact"`; DENY in read-only; DENY without a target |

Both tools are registered only when the session is a member of an enabled
group, never for subagents, never for remote (SSH) turns. Identity comes from
the host binding, not from arguments: the model cannot name its own session,
choose a group, set hops or claim consent.

Consent: `allow_session` binds to `(session.message, target_session_id)`
exactly and lives in an engine-process store keyed by session; it is dropped
on group revision changes that bump the epoch, on session close and on engine
restart. It is never written to the persistent grants file.

Precheck and revalidation: `session.send` declares a `ToolDefinition.precheck`
that runs after schema validation and before policy/approval. A destination
that is missing, closed, outside the group, not receiving, or a sender without
`send`, is rejected there with the same error codes the delivery would raise
(`PEER_NOT_ALLOWED`, `PEER_RECEIVE_DISABLED`, `PEER_TARGET_MISSING`,
`PEER_TARGET_CLOSED`), so the owner is never asked for a consent the call could
not use. The precheck is not the authorization: after approval, `deliver`
re-reads the group and the target state (membership and `authorization_epoch`
may have changed while the prompt was open) and refuses on the fresh snapshot.

### Delivery and provenance

- Message limits: 32 000 chars; 5 sends per turn; 3 hops per chain
  (`PEER_LOOP`); 20 deliveries per chain (`PEER_BUDGET_EXHAUSTED`).
- Same `(source session, source turn, target, text)` within a turn is
  deduplicated (`duplicate: true`).
- The receiver's turn starts with `turn.started.origin` (`messageOrigin`),
  `memory_origin = "automation"` and the model-facing text wrapped as
  "content received from another agent, not the owner's instructions".
- Provenance ceiling (enforced in `ToolRuntime`, not by prompt): a turn whose
  origin is `peer` cannot use `fs.write`, `shell.exec`, `process.local`,
  `browser.mutate`, `network.outbound`, `mcp.call`, `state.write`,
  `git.local`, nor `agent.spawn` / `agent.message` / `agent.synthesize`.
  Reads and discovery stay available. To act on a peer message the owner
  forwards it as their own task (`session.peer_message.forward`, origin
  `user`, no ceiling).
- Stop (`session.turn.cancel`) pauses the target's inbox before the
  cancellation lands; `session.queue.resume` re-enables it.
- An engine restart never replays deliveries accepted by the previous
  process: they become `paused` (`uncertain` when another instance accepted
  them).

### Events

- `session.peer.group.updated` — `peerGroup` (+ `warnings` on set).
- `session.peer.message` — `peerMessage` when a delivery is accepted.
- `session.peer.message.updated` — state transitions (`running`, terminal,
  `paused`, `cancelled`).
- `turn.started` carries `origin` for peer/user-forwarded deliveries.
- `approval.requested` carries `binding_mode` so Code can label the dialog.

## Acceptance (covered by `tests/unit/test_engine_peers.py`)

Group idempotency/revision/epoch/displacement; consent per exact target and
non-leakage to the persistent store; delivery to idle and busy receivers;
denial for non-members, `receive = false`, `send = false`, missing/closed
targets and self-targets before any consent prompt; membership or `receive`
revoked while a consent prompt is open is refused after approval; read-only;
provenance ceiling (shell/write/spawn denied without approval prompts);
hop cut, per-turn rate limit, dedupe; Stop pause + resume; user forward; cancel
of a queued delivery; restart recovery; operations database upgraded from a
pre-peer schema without losing dispatch identities. `tests/unit/test_migrations.py`
covers 0032 over a home written before peer messaging (old rows read back with
no origin).

## Non-goals

- No sustained autonomous coordination between agents: three hops and five
  sends per turn are ceilings, not a workflow.
- No cross-project write coordination: two panels on the same root still
  race; the desktop warns, the engine does not serialize.
- No system notifications: Code owns presentation.
