-- Rinari Agency, «Arma tu equipo con Rinari» (2026-11-25).
-- folders_json: the team's work folders outside its project, trusted by the
-- owner when the team was made. Its posts may write there without asking.
ALTER TABLE agency_teams ADD COLUMN folders_json TEXT NOT NULL DEFAULT '[]';

-- setup_json: what the team still needs before it can work as planned (a
-- connector to connect, a need with no connector yet), each with its agent.
ALTER TABLE agency_teams ADD COLUMN setup_json TEXT NOT NULL DEFAULT '[]';

-- flow_json: what a «flow» task starts with no model (a team's flow and its
-- input). NULL for every other kind of task.
ALTER TABLE scheduled_tasks ADD COLUMN flow_json TEXT;
