# Interactive workspace contract (Protocol v1, additive)

Capabilities: `interactive_questions_v1` and `desktop_workspace_v1`.
Clients must negotiate these before exposing the corresponding actions.

## Questions

The native `user.ask` tool accepts 1–3 questions (`id`, `title`, optional
`options` with `label`, `description`, `recommended`). It is exposed only
when the host supplies a question channel. PLAN can use it through the
ordinary ToolRuntime with `state.read`; it does not grant permissions.

`question.requested`, `question.resolved` and `question.expired` include
`request_id`, `session_id`, `turn_id`, `questions` and `status`. Answers are
strings keyed by question ID. `question.resolve` requires the session and
request IDs, `status: answered | skipped`, and `answers`. Answered requests
must include every question. A recommended choice is never submitted
automatically. Skipping returns an explicit skipped result with no answers.

The engine event store owns history. `question.list({session_id})` returns
pending and historical requests; snapshots contain `pending_questions`.
Cancellation and the tool deadline expire waits. After an engine restart,
orphaned waits expire durably when history is reconstructed, since their
worker no longer exists. Duplicate or expired replies fail without resuming
the model again. Closing a desktop card has no protocol effect.

## Session workspaces

`session.create` with `chat: true` and no explicit `cwd` creates a dedicated
directory under Rinari home `workspaces/`. Explicit CLI cwd semantics remain
unchanged. Managed CHAT workspaces have their own bounded read/write sandbox.

`session.move({session_id, project_id})` rebinds the existing session;
`project_id: null` returns to its general workspace. Active turns, queued
messages, live session processes/PTYs and the shared CLI turn lock block the
transition. No files or repositories are moved. History and artifacts retain
their identities. Project-scoped skills, compact context and Git branch are
recalculated. Old worktree baselines and file/symbol/term pins are recorded
in the move event; baselines are restored only when returning to that same
workspace. Checkpoints retain their original project root. A CLI runtime
already loaded before an external move must resume before executing again.

## File previews

`workspace.file.read({session_id, path, turn_id?})` resolves against the
turn's original workspace when a turn ID is provided. New activity includes
`workspace_root`; historical turns replay workspace transitions. Files must
remain inside that canonical root after resolving symlinks. Engine-private
blobs are excluded. Responses contain `path`, `name`, `language`, `size`, and
UTF-8 `content`; the maximum is 512 KiB. Missing files, unknown provenance,
binary contents and oversize files produce explicit errors.

Existing `artifact.read` handles artifact URIs. Filesystem write/patch
activity also includes `file_path`, independently of truncated arguments,
so clients can link real tool outputs without guessing from prose.
