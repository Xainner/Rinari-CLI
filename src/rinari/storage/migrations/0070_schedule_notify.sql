-- How much a scheduled task notifies the owner (owner, 2026-10-22):
-- all (every run), problems (only a failure or a run that needs them) or none
-- (nothing pops up: the runs stay in its history and the bell).
ALTER TABLE scheduled_tasks ADD COLUMN notify TEXT NOT NULL DEFAULT 'all';
