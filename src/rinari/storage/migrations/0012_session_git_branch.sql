-- 0012: resume reconciliation state.
-- git_branch records the branch checked out when the session started, so a
-- resume can detect a branch switch that happened outside the session.
ALTER TABLE sessions ADD COLUMN git_branch TEXT;