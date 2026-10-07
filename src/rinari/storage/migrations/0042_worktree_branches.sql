-- 0042: the branch-change warning compared the checkout with the branch a
-- conversation started on, captured once and never advanced. Reopening an old
-- conversation repeated "A -> main" long after the work had moved on. This
-- keeps, per git worktree, the branch last observed while Rinari worked
-- there (any conversation), with where the observation came from.
-- sessions.git_branch stays as the historical start of each conversation.
CREATE TABLE IF NOT EXISTS worktree_branches (
    worktree TEXT PRIMARY KEY,
    branch TEXT,
    head TEXT,
    observed_at TEXT NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    source TEXT NOT NULL,
    notified TEXT
);
