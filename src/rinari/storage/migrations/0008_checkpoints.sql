-- Phase 3: checkpoints of agent-owned worktree changes for `rinari undo`.
-- NOTE: this file must not contain semicolons inside comments because the
-- migration runner splits statements on the semicolon character.

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    session_ref TEXT NOT NULL,
    project_root TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    agent_changes INTEGER NOT NULL DEFAULT 0,
    user_owned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoint_files (
    checkpoint_id TEXT NOT NULL,
    path TEXT NOT NULL,
    ownership TEXT NOT NULL,
    prior_source TEXT NOT NULL DEFAULT 'snapshot',
    prior_status TEXT NOT NULL DEFAULT '',
    prior_blob BLOB,
    PRIMARY KEY (checkpoint_id, path)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_session
    ON checkpoints(session_ref, created_at);

CREATE INDEX IF NOT EXISTS idx_checkpoints_project
    ON checkpoints(project_root, created_at);