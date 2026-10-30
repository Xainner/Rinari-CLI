-- Plan 0.4.0 #75: the memory manager says where a memory came from (the
-- conversation that saved it) and which ones have not been used for a while
-- (last time it went into a prompt). The origin stays NULL for older records
-- and their last use starts at their last change, so nothing reads as stale
-- just because of the upgrade.
-- NOTE: no semicolons inside comments, the runner splits on them.
ALTER TABLE user_memory ADD COLUMN source_session_id TEXT;

ALTER TABLE user_memory ADD COLUMN last_used_at TEXT;

ALTER TABLE project_memory ADD COLUMN source_session_id TEXT;

ALTER TABLE project_memory ADD COLUMN last_used_at TEXT;

UPDATE user_memory SET last_used_at = updated_at WHERE last_used_at IS NULL;

UPDATE project_memory SET last_used_at = updated_at WHERE last_used_at IS NULL;
