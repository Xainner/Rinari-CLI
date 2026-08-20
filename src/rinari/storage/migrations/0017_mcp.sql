-- 0017: MCP server registry (phase 5).
-- Configured MCP servers (stdio or HTTP transport) plus per-scope trust.
CREATE TABLE mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    transport TEXT NOT NULL,
    command TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(scope, name)
);
CREATE INDEX idx_mcp_servers_enabled ON mcp_servers(enabled);