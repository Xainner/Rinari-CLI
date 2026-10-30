-- Rinari Agency A2: teams. A team is an office with its chat, its board of
-- errands, its memory, its accounts, its budget and its chief. An agent joins
-- a team through a post: a CHAT session marked with both agent_id and
-- team_id, where the errands it takes run, one at a time. Times are epoch
-- seconds, like the agents.
-- NOTE: no semicolons inside comments, the runner splits on them.
CREATE TABLE agency_teams (
    id TEXT PRIMARY KEY,
    rinari_profile_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    chief_agent_id TEXT,
    connectors_json TEXT NOT NULL DEFAULT '[]',
    budget_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived_at REAL
);

CREATE INDEX idx_agency_teams_profile ON agency_teams(rinari_profile_id, archived_at);

-- One post per agent and team. The post's session is where its errands run.
CREATE TABLE agency_posts (
    team_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (team_id, agent_id)
);

ALTER TABLE sessions ADD COLUMN team_id TEXT;

CREATE INDEX idx_sessions_team ON sessions(team_id);

-- The office chat: a group chat of the owner and the team's agents. It is not
-- a model conversation. A mention wakes the mentioned agent in its post.
-- chain_id and hop are counted by the Engine from the message that caused
-- them, never taken from the model.
CREATE TABLE agency_office_messages (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    author_kind TEXT NOT NULL,
    agent_id TEXT,
    text TEXT NOT NULL,
    mentions_json TEXT NOT NULL DEFAULT '[]',
    errand_id TEXT,
    chain_id TEXT NOT NULL,
    hop INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE INDEX idx_agency_office_team ON agency_office_messages(team_id, created_at);

-- Errands: the board is the queue. An errand to nobody goes to the chief.
-- done_when is the list of how to know it is done. One errand per post at a
-- time (the first way, D-28).
CREATE TABLE agency_errands (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    from_team_id TEXT,
    from_agent_id TEXT,
    to_agent_id TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    done_when_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    result_json TEXT,
    return_reason TEXT,
    chain_id TEXT NOT NULL,
    hop INTEGER NOT NULL DEFAULT 0,
    production_run_id TEXT,
    session_id TEXT,
    turn_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    delivered_at REAL
);

CREATE INDEX idx_agency_errands_queue ON agency_errands(team_id, status, created_at);
