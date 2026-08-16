-- 0001: initial schema (Fase 1 foundations)
-- schema_migrations is owned by the runner, not by any migration file.

CREATE TABLE providers (
  id TEXT PRIMARY KEY,
  alias TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL,
  auth_method TEXT NOT NULL,
  account_hint TEXT,
  endpoint TEXT,
  settings_json TEXT NOT NULL DEFAULT '{}',
  default_model_id TEXT,
  last_used_model_id TEXT,
  status_connected INTEGER,
  status_checked_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE provider_credentials_metadata (
  provider_id TEXT PRIMARY KEY REFERENCES providers(id) ON DELETE CASCADE,
  secret_ref TEXT NOT NULL,
  method TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE models (
  id TEXT PRIMARY KEY,
  alias TEXT NOT NULL,
  provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
  provider_model_id TEXT NOT NULL,
  settings_json TEXT NOT NULL DEFAULT '{}',
  capabilities_json TEXT,
  availability TEXT NOT NULL DEFAULT 'unknown',
  created_at TEXT NOT NULL,
  updated_at NOT NULL,
  UNIQUE (provider_id, alias)
);

CREATE INDEX idx_models_provider ON models(provider_id);

CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  canonical_root TEXT NOT NULL UNIQUE,
  git_fingerprint TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('CHAT', 'PROJECT')),
  title TEXT,
  project_id TEXT REFERENCES projects(id) ON DELETE RESTRICT,
  project_root_snapshot TEXT,
  created_cwd TEXT NOT NULL,
  current_cwd TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  profile_id TEXT NOT NULL DEFAULT 'default',
  mode TEXT NOT NULL DEFAULT 'ask',
  state TEXT NOT NULL DEFAULT 'active',
  compact_state_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_active_at TEXT NOT NULL
);

CREATE INDEX idx_sessions_kind ON sessions(kind, last_active_at DESC);
CREATE INDEX idx_sessions_project ON sessions(project_id, last_active_at DESC);
CREATE INDEX idx_sessions_state ON sessions(state, last_active_at DESC);

CREATE TABLE session_events (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE (session_id, seq)
);

CREATE TABLE config_values (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);