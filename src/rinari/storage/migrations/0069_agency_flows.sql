-- Rinari Agency, replan of 2026-10-22 (phases 2 and 3).
-- manual: the team's procedure, written by the owner, read by every post on
-- every turn (before, it was a message lost in the office).
ALTER TABLE agency_teams ADD COLUMN manual TEXT NOT NULL DEFAULT '';

-- A flow: steps that hand work on in order, each with who does it and who
-- approves it. steps_json: [{name, agents: [agent_id], instructions,
-- approval: auto | chief | owner}].
CREATE TABLE agency_flows (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    steps_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX idx_agency_flows_team ON agency_flows(team_id);

-- One run of a flow: the step it is on and what each step delivered.
-- status: running, done, cancelled. outputs_json: [{step, agent_id, errand_id,
-- summary}].
CREATE TABLE agency_flow_runs (
    id TEXT PRIMARY KEY,
    flow_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    status TEXT NOT NULL,
    step INTEGER NOT NULL DEFAULT 0,
    input TEXT NOT NULL DEFAULT '',
    outputs_json TEXT NOT NULL DEFAULT '[]',
    started_by TEXT,
    chain_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);

CREATE INDEX idx_agency_flow_runs_team ON agency_flow_runs(team_id, status);

-- The step of its flow run an errand does (production_run_id names the run).
ALTER TABLE agency_errands ADD COLUMN flow_step INTEGER;
