-- Plan 0.4.0, child sessions (plan-sesiones-hijas section 5): a conversation Rinari opens or
-- offers for a separate piece of work. origin is start (the owner asked), offer (the owner
-- accepted an offer), note or plan_step (the owner started a card). context is brief or fork.
-- worktree_path and branch are set when the child works in its own git worktree.
-- state follows the child's last turn that the owner did not drive: working, completed,
-- failed, cancelled or waiting. report_json is its last report to the mother and reported
-- says whether the mother already saw it. resolution is integrated or discarded for a branch.
-- NOTE: no semicolons in comments (the runner splits on them).
CREATE TABLE IF NOT EXISTS session_children (
  child_session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  parent_session_id TEXT NOT NULL,
  origin TEXT NOT NULL,
  context TEXT NOT NULL DEFAULT 'brief',
  worktree_path TEXT,
  branch TEXT,
  base_commit TEXT,
  external_content INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'working',
  report_json TEXT,
  reported INTEGER NOT NULL DEFAULT 1,
  resolution TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_children_parent ON session_children(parent_session_id);
-- A note is a plain note or a plan_step card (one per independent step of an approved
-- plan). Plan steps keep their own caps and never supersede notes.
ALTER TABLE followup_suggestions ADD COLUMN kind TEXT NOT NULL DEFAULT 'note';
