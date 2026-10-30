-- Plan 0.4.0 #75: where a personal memory applies. general (everywhere, what
-- every memory was until now), profile (only with that Rinari profile active,
-- place_ref is its id) or chat (only in that conversation, place_ref is the
-- session id, and it goes away with the conversation). Project memory keeps
-- its own table. A candidate remembers the place the model chose, which the
-- owner can change on the card before approving. NOTE: no semicolons inside
-- comments, the runner splits on them.
ALTER TABLE user_memory ADD COLUMN place TEXT NOT NULL DEFAULT 'general';

ALTER TABLE user_memory ADD COLUMN place_ref TEXT;

CREATE INDEX IF NOT EXISTS idx_user_memory_place ON user_memory(place, place_ref);

ALTER TABLE memory_candidates ADD COLUMN place TEXT;

ALTER TABLE memory_candidates ADD COLUMN place_ref TEXT;
