-- Rinari Agency A1: agents that work alone. An agent belongs to a Rinari
-- profile (each profile has its own Agency) and has a card (name, role,
-- instructions, model, permission mode, allowed connectors, daily budget) and
-- a direct chat, which is an ordinary CHAT session marked with agent_id.
-- Times are epoch seconds, like scheduled tasks.
-- NOTE: no semicolons inside comments, the runner splits on them.
CREATE TABLE agency_agents (
    id TEXT PRIMARY KEY,
    rinari_profile_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    model TEXT,
    permission_profile TEXT NOT NULL DEFAULT 'workspace',
    connectors_json TEXT NOT NULL DEFAULT '[]',
    budget_json TEXT NOT NULL DEFAULT '{}',
    ask_new_destinations INTEGER,
    session_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived_at REAL
);

CREATE INDEX idx_agency_agents_profile ON agency_agents(rinari_profile_id, archived_at);

-- The agent a conversation belongs to (its direct chat). NULL for every
-- conversation of Rinari herself, which keeps them apart in session.list.
ALTER TABLE sessions ADD COLUMN agent_id TEXT;

CREATE INDEX idx_sessions_agent ON sessions(agent_id);

-- A permanent rule is a scheduled task of an agent. tool_json holds what a
-- «tool» task calls without the model (a connector tool with fixed
-- arguments) and where its result goes.
ALTER TABLE scheduled_tasks ADD COLUMN agent_id TEXT;

ALTER TABLE scheduled_tasks ADD COLUMN tool_json TEXT;

-- A rule can watch a folder: on each check it runs only when the folder
-- changed. watch_json holds the folder.
ALTER TABLE scheduled_tasks ADD COLUMN watch_json TEXT;

-- Plan de conectores C8: an account that belongs to an agent (agent:ID).
-- Empty means everyone, as before.
ALTER TABLE mcp_servers ADD COLUMN owner TEXT NOT NULL DEFAULT '';
