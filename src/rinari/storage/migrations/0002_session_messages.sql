-- 0002: persisted conversation (phase 2 closeout).
-- Conversation state is provider-agnostic ChatMessage rows (harness.md 28):
-- resume restores history across invocations and a CHAT -> PROJECT promotion
-- preserves it by definition (same session id).

CREATE TABLE IF NOT EXISTS session_messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT,
  tool_calls_json TEXT,
  tool_call_id TEXT,
  name TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_session_messages_session
  ON session_messages (session_id, seq);