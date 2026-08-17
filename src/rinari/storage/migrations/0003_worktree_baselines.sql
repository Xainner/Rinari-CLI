-- 0003: worktree baselines (phase 3, dirty-worktree protection).
-- Per-session snapshot of the pre-existing uncommitted state so writes can
-- be flagged (and gated behind approval) when they touch user-owned work.

CREATE TABLE IF NOT EXISTS worktree_baselines (
  session_id TEXT NOT NULL,
  path TEXT NOT NULL,
  git_status TEXT NOT NULL,
  blob_sha TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (session_id, path)
);