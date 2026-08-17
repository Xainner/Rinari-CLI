-- Phase 4: artifact store metadata (file-backed content in the artifacts dir).
-- NOTE: this file must not contain semicolons inside comments because the
-- migration runner splits statements on the semicolon character.

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT NOT NULL,
    session_ref TEXT NOT NULL,
    project_root TEXT NOT NULL DEFAULT '',
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    sha256 TEXT NOT NULL,
    byte_count INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    provenance TEXT NOT NULL DEFAULT '',
    retention TEXT NOT NULL DEFAULT 'session',
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_ref, namespace, id)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_project
    ON artifacts(project_root, created_at);