-- The goal of a conversation (plan 0.4.0 #23): an objective Rinari pursues
-- turn after turn until it is achieved. One row per session. state:
-- pursuing while it runs, paused by the owner or a stop, waiting_limits
-- until a provider limit resets, achieved when Rinari says so with evidence
-- and the verification agrees, blocked when it needs the owner, stalled
-- after iterations without progress, budget_limited when a cap the owner set
-- ran out, cleared when ended. budget_json holds the owner's optional caps
-- (turns, minutes, tokens), empty means none. NOTE: no semicolons in
-- comments (the runner splits on them).
CREATE TABLE IF NOT EXISTS session_goals (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  goal_id TEXT NOT NULL,
  objective TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pursuing'
    CHECK (state IN ('pursuing', 'paused', 'waiting_limits', 'achieved', 'blocked',
                     'stalled', 'budget_limited', 'cleared')),
  budget_json TEXT NOT NULL DEFAULT '{}',
  iteration INTEGER NOT NULL DEFAULT 0,
  tokens_used INTEGER NOT NULL DEFAULT 0,
  active_seconds REAL NOT NULL DEFAULT 0,
  quiet_iterations INTEGER NOT NULL DEFAULT 0,
  assessment_json TEXT NOT NULL DEFAULT '{}',
  reason TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
