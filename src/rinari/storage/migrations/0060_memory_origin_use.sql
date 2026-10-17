-- Plan 0.4.0 #75: the memory manager says where a memory came from (the
-- conversation that saved it) and which ones have not been used for a while
-- (last time it went into a prompt). Both stay NULL for older records.
-- NOTE: no semicolons inside comments, the runner splits on them.
ALTER TABLE user_memory ADD COLUMN source_session_id TEXT;

ALTER TABLE user_memory ADD COLUMN last_used_at TEXT;

ALTER TABLE project_memory ADD COLUMN source_session_id TEXT;

ALTER TABLE project_memory ADD COLUMN last_used_at TEXT;
