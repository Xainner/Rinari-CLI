CREATE TABLE IF NOT EXISTS turn_changesets (
  id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL UNIQUE,
  session_id TEXT NOT NULL,
  project_id TEXT,
  roots_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  additions INTEGER NOT NULL DEFAULT 0,
  deletions INTEGER NOT NULL DEFAULT 0,
  undoable INTEGER NOT NULL DEFAULT 0,
  attribution_complete INTEGER NOT NULL DEFAULT 1,
  warnings_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS turn_changed_files (
  changeset_id TEXT NOT NULL,
  path TEXT NOT NULL,
  absolute_path TEXT NOT NULL,
  previous_path TEXT,
  kind TEXT NOT NULL,
  additions INTEGER,
  deletions INTEGER,
  before_exists INTEGER NOT NULL,
  after_exists INTEGER NOT NULL,
  before_hash TEXT,
  after_hash TEXT,
  before_size INTEGER,
  after_size INTEGER,
  ownership TEXT NOT NULL,
  confidence TEXT NOT NULL,
  binary INTEGER NOT NULL DEFAULT 0,
  sensitive INTEGER NOT NULL DEFAULT 0,
  diff_text TEXT,
  diff_truncated INTEGER NOT NULL DEFAULT 0,
  undoable INTEGER NOT NULL DEFAULT 0,
  conflict_reason TEXT,
  before_blob_ref TEXT,
  PRIMARY KEY(changeset_id, absolute_path),
  FOREIGN KEY(changeset_id) REFERENCES turn_changesets(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS turn_change_undo_operations (
  id TEXT PRIMARY KEY,
  changeset_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  status TEXT NOT NULL,
  applied_json TEXT NOT NULL DEFAULT '[]',
  conflicts_json TEXT NOT NULL DEFAULT '[]',
  skipped_json TEXT NOT NULL DEFAULT '[]',
  FOREIGN KEY(changeset_id) REFERENCES turn_changesets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_turn_changesets_session
  ON turn_changesets(session_id, completed_at);
