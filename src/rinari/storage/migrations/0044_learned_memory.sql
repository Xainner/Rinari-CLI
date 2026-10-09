-- Learned facts (memory.propose). A proposal may target project memory, so a
-- candidate keeps the store it is meant for and the project it belongs to
-- until the owner approves it. Project records gain the optimistic revision
-- user records already have, so the desktop edits and forgets both the same
-- way. NOTE: no semicolons inside comments, the runner splits on them.
ALTER TABLE memory_candidates ADD COLUMN scope TEXT NOT NULL DEFAULT 'user';

ALTER TABLE memory_candidates ADD COLUMN project_root TEXT;

ALTER TABLE project_memory ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
