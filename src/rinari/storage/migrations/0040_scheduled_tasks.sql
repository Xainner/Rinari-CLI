-- 0040 (shipped briefly as 0037): scheduled tasks and their runs. A task says when (schedule, in the
-- machine's local time), what (a reminder, or a prompt for an agent turn in a
-- project or a chat), how (mode, model, skills) and what it may do without
-- asking (grants approved when it was created). Times are epoch seconds.
CREATE TABLE scheduled_tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'agent',
    schedule_json TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    project_id TEXT,
    mode TEXT NOT NULL DEFAULT 'build',
    model TEXT,
    skills_json TEXT NOT NULL DEFAULT '[]',
    grants_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE scheduled_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'schedule',
    scheduled_for REAL,
    started_at REAL,
    finished_at REAL,
    session_id TEXT,
    turn_id TEXT,
    summary TEXT,
    reason TEXT
);

CREATE INDEX scheduled_runs_task ON scheduled_runs(task_id, started_at);
CREATE INDEX scheduled_runs_session ON scheduled_runs(session_id);
