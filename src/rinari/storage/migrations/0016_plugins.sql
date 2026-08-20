-- 0016: plugin registry (phase 5).
-- Installed plugins and their recorded state. The manifest JSON holds the
-- requested capabilities so install/enable can preview them before load.
CREATE TABLE plugins (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    version TEXT NOT NULL,
    source TEXT NOT NULL,
    scope TEXT NOT NULL,
    path TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    manifest_json TEXT NOT NULL,
    diagnosed TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(scope, name)
);
CREATE INDEX idx_plugins_enabled ON plugins(enabled);