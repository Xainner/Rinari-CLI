# 01 — Project workspace protocol

> Status (2026-09-09): P1 `project.list_recent/open/status` implemented
> and pinned in `tests/unit/test_engine_project_workspace.py`. One
> deviation: recents carry identity + binding only — `branch`/`dirty`
> live in `project.status` (single git-truth source for the dashboard
> header) to keep the application layer free of engine imports.

## What Code needs

Rinari Code must open on a project home (recent projects, dashboard,
intelligence) backed by the engine. Today the desktop only derives a
project subtitle from `project_root_snapshot` in `session.list`. There is
no `project.*` surface beyond `project.changes` / `project.diff`.

## Current engine state

- `ProjectService` (`src/rinari/application/project_service.py`) exposes
  only `upsert(root)` and `init(...)`. No listing, no open, no status.
- `ProjectRecord` (`src/rinari/storage/records.py`) already carries
  `id`, `canonical_root`, `git_fingerprint`, `metadata`, timestamps —
  enough identity to list recents without schema churn.
- `session.list` exposes `project_root_snapshot` per session (Code uses
  this as a hint, not as project truth).

## Proposal

### `project.list_recent`

```json
{ "id": "req_1", "method": "project.list_recent", "params": { "limit": 20 } }
```

```json
{
  "id": "req_1",
  "ok": true,
  "result": {
    "projects": [
      {
        "id": "prj_...",
        "root": "/abs/path",
        "git_fingerprint": "sha / null",
        "branch": "main",
        "dirty": false,
        "last_opened_at": "2026-09-09T...",
        "active_session_id": "ses_... | null"
      }
    ]
  }
}
```

Ordering: most recently opened first. `last_opened_at` is engine-tracked
(open/session activity), never scanned by the desktop.

### `project.open`

```json
{ "id": "req_2", "method": "project.open", "params": { "path": "/abs/path" } }
```

Upserts the project, returns the project view plus the recommended
session (existing active session for that root, or a newly created
PROJECT session). Reuses `SessionService` promotion semantics: same
session identity, conversation, provider and model preserved.

### `project.status`

```json
{ "id": "req_3", "method": "project.status", "params": { "path": "/abs/path" } }
```

Returns branch/head/dirty/file list (the shape behind today's
`project.changes`) plus session binding. This is the single source for
the desktop dashboard header.

### `project.intelligence` (P2)

Read-only repository understanding the engine already computes elsewhere
(languages, frameworks, test/lint setup, index stats, active instruction
scopes). No new scanning architecture: project only what already exists
behind_repo index / instructions / LSP state. Fields the engine cannot
compute are omitted, never fabricated.

## Acceptance criteria

- [ ] `project.list_recent` reflects real open activity across CLI and
      desktop (shared home, no second store).
- [ ] `project.open` on an already-open root returns the same project
      and reuses (not duplicates) the active session.
- [ ] `project.status` matches `git status` truth; desktop renders it
      without independent scanning.
- [ ] Unknown `metadata` keys pass through untouched.

## Non-goals

- No desktop-side directory scanning or project DB.
- No clone/scaffold flows in v1 (open existing roots only).
- No intelligence fields the engine cannot actually compute.
