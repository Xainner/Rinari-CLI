-- Phase 4: separate memory stores (harness.md section 69: never merged).
-- user memory is available to CHAT and PROJECT. project memory is scoped to
-- its project. episodic holds summaries of prior tasks (historical evidence).
-- pattern holds reusable procedure knowledge. All records require provenance
-- and pass a sensitivity filter at write time (no secrets).
-- NOTE: this file must not contain semicolons inside comments because the
-- migration runner splits statements on the semicolon character.

CREATE TABLE IF NOT EXISTS user_memory (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'preference',
    topic TEXT NOT NULL,
    text TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_memory_topic
    ON user_memory(kind, topic);

CREATE TABLE IF NOT EXISTS project_memory (
    id TEXT PRIMARY KEY,
    project_root TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'fact',
    topic TEXT NOT NULL,
    text TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_memory_root
    ON project_memory(project_root, topic);

CREATE TABLE IF NOT EXISTS episodic_memory (
    id TEXT PRIMARY KEY,
    session_ref TEXT NOT NULL,
    project_root TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',
    provenance TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_episodic_project
    ON episodic_memory(project_root);

CREATE TABLE IF NOT EXISTS pattern_memory (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'global',
    topic TEXT NOT NULL,
    text TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pattern_scope
    ON pattern_memory(scope, topic);