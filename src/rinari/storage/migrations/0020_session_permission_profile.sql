-- Per-session BUILD permission preference. PLAN and REVIEW remain read-only.
ALTER TABLE sessions ADD COLUMN permission_profile TEXT NOT NULL DEFAULT 'workspace';
