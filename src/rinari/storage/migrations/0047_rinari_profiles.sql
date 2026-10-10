-- Rinari profiles are also workspaces: every project and conversation belongs
-- to one. Everything that existed before lands in the built-in default
-- profile, so nothing becomes hidden. The name avoids profile_id, which
-- sessions already use for the CLI config profile. A project's conversations
-- always share its profile (enforced in the services, the runner cannot
-- create triggers). NOTE: no semicolons in comments.
ALTER TABLE projects ADD COLUMN rinari_profile_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE sessions ADD COLUMN rinari_profile_id TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_projects_rinari_profile ON projects(rinari_profile_id, archived);
CREATE INDEX IF NOT EXISTS idx_sessions_rinari_profile ON sessions(rinari_profile_id, kind, last_active_at);
