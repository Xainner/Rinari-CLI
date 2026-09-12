# Durable Engine operations

Additive protocol-v1 capability: `durable_operations_v1`. Existing `session.turn.start` remains unchanged. Clients must check the capability before using these methods.

`operation.start` accepts `operation_id` (nonempty string, at most 128 characters), `session_id`, nonblank `message`, and optional `reasoning_effort` (`low`, `medium`, `high`). It returns `{operation_id, session_id, turn_id, state, updated}`. The fingerprint includes session, exact message and effort. Reusing an ID with a different fingerprint fails with `INVALID_PARAMS`; identical requests return the existing operation without starting another worker.

`operation.get` accepts `operation_id`, returning `{operation: record|null}`. `operation.cancel` accepts the same identity, requests cancellation of its exact turn, and returns the current record. Unknown cancellation fails with `INVALID_PARAMS`. Cancellation never selects a later turn merely because it shares the session.

The Engine atomically reserves the identity in `<RINARI_HOME>/engine-operations.sqlite` before starting the worker. The independent SQLite schema is version 1; unknown versions are refused. No prompt or result text is stored here, only its fingerprint and lifecycle references. The normal Engine history remains authoritative for content and tools continue through the existing runtime/policies.

States are `running`, `cancelling`, `completed`, `failed`, `cancelled`, `stopped`, and `uncertain`. A terminal state is persisted before its terminal event is emitted. A pending record from a previous Engine instance is reported as `uncertain`, never resumed or replayed by `operation.start`. A failure during initial dispatch is also conservatively uncertain. This is deduplicated dispatch while the identity ledger is retained, not a transaction spanning external tool effects. A crash may leave partial effects requiring review.

The ledger is additive: existing session databases are not migrated. Back up the complete Engine home while stopped, including the ledger, together with any coordinating Gateway state. Do not delete the ledger to retry a job. Deleting/restoring identities independently removes the historical deduplication guarantee. No pruning, reset API, external-effect rollback or cross-version compatibility claim is introduced here.

Tests cover protocol registration, duplicate/conflicting start, one model invocation, terminal persistence on reopen, uncertain ownership after restart, cancellation and existing turn/runtime behavior. Publication into a pinned Gateway bundle requires committing this revision and separately validating an upgrade path; editing this checkout does not update deployed installations.


SSH extension: `ssh_targets_v1` adds `target.list` and immutable `target.add` with
`id`, `name`, literal IP `host`, integer `port`, `username`, identity filename and
`host_key` (`ssh-ed25519 BASE64`). The record includes a canonical SHA-256 `revision`.
`operation.start` accepts `target_id` and `target_revision`; non-gateway targets must
exist with that exact revision and require CHAT. The destination snapshot is included
in the operation fingerprint; old local-operation fingerprints remain compatible.
SSH records use a separate additive schema-v1 database at `ssh/targets.sqlite`.
Provision identities under `ssh/identities` in the installation home. No private key
contents travel through the target API and no SSH credentials are imported implicitly.
