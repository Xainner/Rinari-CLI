-- Phase 3: externalized task graph (commands.md 31) + done-when contract.
-- Tasks are project-scoped by project_root. The session_ref column is
-- reserved for the in-session wiring (completion gate / context engine).
-- NOTE: the migration runner splits statements on semicolons, so comments
-- in this file must not contain any.

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_root TEXT NOT NULL,
    session_ref TEXT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    acceptance TEXT NOT NULL DEFAULT '',
    implementation TEXT NOT NULL DEFAULT '',
    validation TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    unresolved TEXT NOT NULL DEFAULT '',
    depends_on TEXT NOT NULL DEFAULT '',
    blockers TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_project_root ON tasks(project_root);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_ref);