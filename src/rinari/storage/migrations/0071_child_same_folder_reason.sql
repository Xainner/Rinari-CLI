-- Why a child that could have had its own branch works in the project folder
-- (not_repo, project_prefers_same, git_timeout, worktree_failed): its card
-- says so instead of a silent fallback (owner's demo, 2026-11-01).
ALTER TABLE session_children ADD COLUMN same_folder_reason TEXT;
