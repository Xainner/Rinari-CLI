-- Rinari Agency, review of A2: the caps of the office and the board.
-- system marks what the Engine posts in the office (an errand left, a wait),
-- which never counts as a teammate's answer. notice_json says what such a
-- message is, for the app to word it in the owner's language. external
-- marks text that comes from a turn that read something from outside the
-- machine, so the turn it wakes keeps the guard on.
-- NOTE: no semicolons inside comments, the runner splits on them.
ALTER TABLE agency_office_messages ADD COLUMN system INTEGER NOT NULL DEFAULT 0;

ALTER TABLE agency_office_messages ADD COLUMN notice_json TEXT;

ALTER TABLE agency_office_messages ADD COLUMN external INTEGER NOT NULL DEFAULT 0;

-- deliveries counts each delivery of an errand (the chain cap adds them up)
-- and returns each time it was sent back (3 at most, then it is blocked).
ALTER TABLE agency_errands ADD COLUMN deliveries INTEGER NOT NULL DEFAULT 0;

ALTER TABLE agency_errands ADD COLUMN returns INTEGER NOT NULL DEFAULT 0;

ALTER TABLE agency_errands ADD COLUMN external INTEGER NOT NULL DEFAULT 0;

-- A post that waits for a teammate (office.wait), kept across restarts.
CREATE TABLE agency_waits (
    session_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    for_agent_id TEXT NOT NULL,
    chain_id TEXT NOT NULL,
    hop INTEGER NOT NULL DEFAULT 0,
    message_at REAL NOT NULL DEFAULT 0,
    since REAL NOT NULL
);
