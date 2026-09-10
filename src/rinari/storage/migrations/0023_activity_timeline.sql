-- 0023: narrative activity timeline correlation.
-- Nullable columns keep every existing session/event readable.

ALTER TABLE session_events ADD COLUMN turn_id TEXT;
ALTER TABLE session_events ADD COLUMN activity_seq INTEGER;
ALTER TABLE session_messages ADD COLUMN turn_id TEXT;

CREATE INDEX IF NOT EXISTS idx_session_events_timeline
  ON session_events (session_id, turn_id, activity_seq, seq);

CREATE INDEX IF NOT EXISTS idx_session_messages_turn
  ON session_messages (session_id, turn_id, seq);
