-- The live checklist of a conversation: the steps the model is carrying out,
-- kept current with checklist.update. One row per session holds the current
-- list, the turn events hold its history. state: active while a turn works
-- on it, open or interrupted when a turn ended with steps left, completed
-- when every step was done, cleared when emptied or rolled over. NOTE: no
-- semicolons in comments (the runner splits on them).
CREATE TABLE IF NOT EXISTS session_checklists (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  turn_id TEXT,
  revision INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'active'
    CHECK (state IN ('active', 'open', 'interrupted', 'completed', 'cleared')),
  items_json TEXT NOT NULL DEFAULT '[]',
  explanation TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_checklists_turn ON session_checklists(turn_id);
