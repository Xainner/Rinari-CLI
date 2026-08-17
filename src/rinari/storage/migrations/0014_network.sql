-- 0014: network policy foundation (phase 4).
-- Persistent allow/deny rules by host plus an audit trail of the
-- decisions the runtime gate takes for network-facing tools.
-- No semicolons allowed inside comments: the migration runner splits on them.
CREATE TABLE network_rules (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    project_id TEXT,
    host TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(scope, host, decision)
);
CREATE INDEX idx_network_rules_scope ON network_rules(scope);
CREATE TABLE network_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_network_events_created ON network_events(created_at);