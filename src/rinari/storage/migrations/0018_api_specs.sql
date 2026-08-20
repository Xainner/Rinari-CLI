-- 0018: OpenAPI spec registry (phase 5).
-- Registered API specs and the generated-operation metadata.
CREATE TABLE api_specs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    origin TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    auth_json TEXT NOT NULL DEFAULT '',
    overrides_json TEXT NOT NULL DEFAULT '',
    spec_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(scope, name)
);
CREATE INDEX idx_api_specs_enabled ON api_specs(enabled);