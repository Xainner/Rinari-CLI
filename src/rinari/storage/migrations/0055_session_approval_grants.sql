-- Plan 0.4.0 #56: «in this chat» and «in this session» approvals are kept with the
-- conversation, so they survive restarting the app, and go with it when it is deleted.
-- NOTE: no semicolons in comments (the runner splits on them).
CREATE TABLE IF NOT EXISTS session_approval_grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  capability TEXT NOT NULL,
  rule_id TEXT,
  target TEXT,
  project_id TEXT,
  granted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_approval_grants_session ON session_approval_grants(session_id);
