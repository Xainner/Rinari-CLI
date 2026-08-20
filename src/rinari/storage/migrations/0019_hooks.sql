-- 0019: registry of declared lifecycle hooks (phase 5).
-- A row is a *declaration* (event, source, handler type + reference,
-- requested capabilities). Execution is gated by trust + capability checks.
CREATE TABLE hooks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    event TEXT NOT NULL,
    source TEXT NOT NULL,
    scope TEXT NOT NULL,
    handler_type TEXT NOT NULL,
    handler_ref TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    risk TEXT NOT NULL DEFAULT 'low',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(scope, source, name)
);
CREATE INDEX idx_hooks_event ON hooks(event);
CREATE INDEX idx_hooks_enabled ON hooks(enabled);