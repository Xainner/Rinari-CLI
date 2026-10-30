-- Continue a conversation by itself when a provider limit resets (plan 0.4.0
-- #24). One row per session. mode: at when the provider said when the limit
-- resets, poll when only the balance can tell (credit-only providers). state:
-- waiting until it is due, done once a turn continued, cancelled by the
-- owner, failed after too many attempts, expired when the engine was off for
-- more than 12 hours past the time. NOTE: no semicolons in comments (the
-- runner splits on them).
CREATE TABLE IF NOT EXISTS session_resumes (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  provider_id TEXT,
  mode TEXT NOT NULL DEFAULT 'at' CHECK (mode IN ('at', 'poll')),
  resume_at REAL NOT NULL,
  state TEXT NOT NULL DEFAULT 'waiting'
    CHECK (state IN ('waiting', 'done', 'cancelled', 'failed', 'expired')),
  attempts INTEGER NOT NULL DEFAULT 0,
  goal_id TEXT,
  reason TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_resumes_due ON session_resumes(state, resume_at);
