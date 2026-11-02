-- Rinari Agency: a team can work in a project (owner, 2026-11-02). Its posts
-- open in that project's folder instead of an empty one of their own, so an
-- errand that names a relative path finds it. NULL: no project.
ALTER TABLE agency_teams ADD COLUMN project_id TEXT;
