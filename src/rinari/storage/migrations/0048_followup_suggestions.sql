-- Follow-up suggestions: short tasks Rinari leaves as a note while she works
-- (followup.suggest). A note never starts work by itself. Accepting one opens
-- a new conversation in the same project and profile and starts it. The
-- profile is the session's or project's, so moving work needs no update
-- here. NOTE: no semicolons in comments.
CREATE TABLE IF NOT EXISTS followup_suggestions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  turn_id TEXT,
  project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  prompt TEXT NOT NULL,
  rationale TEXT NOT NULL DEFAULT '',
  normalized_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'accepted', 'dismissed', 'superseded', 'expired')),
  provenance_json TEXT NOT NULL DEFAULT '{}',
  accepted_session_id TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_followups_session ON followup_suggestions(session_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_followups_project ON followup_suggestions(project_id, status, created_at);
