-- Rinari Agency, replan of 2026-10-22.
-- The turns queued in each post (mentions, released waits, reviews of a
-- delivery), so an Engine restart or a stop and resume does not lose them.
-- items_json: the post's queue in order, as the board keeps it.
CREATE TABLE agency_queue (
    session_id TEXT PRIMARY KEY,
    items_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
