-- Phase 3: persistent validation evidence for the completion gate.
-- NOTE: this file must not contain semicolons inside comments because the
-- migration runner splits statements on the semicolon character.

CREATE TABLE IF NOT EXISTS validation_records (
    id TEXT PRIMARY KEY,
    project_root TEXT NOT NULL,
    session_ref TEXT,
    kind TEXT NOT NULL,
    command TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    artifact_ref TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_validation_project
    ON validation_records(project_root, created_at);

CREATE INDEX IF NOT EXISTS idx_validation_kind
    ON validation_records(project_root, kind, created_at);