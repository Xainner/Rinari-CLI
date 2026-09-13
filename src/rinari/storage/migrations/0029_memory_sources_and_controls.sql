-- Durable provenance and privacy controls for selective personal memory.
-- Source rows are references, not a second copy of the conversation authority.
CREATE TABLE IF NOT EXISTS memory_sources (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    quote TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_sources_memory
    ON memory_sources(memory_id, revoked_at);

CREATE INDEX IF NOT EXISTS idx_memory_sources_message
    ON memory_sources(session_id, message_id, revoked_at);

CREATE TABLE IF NOT EXISTS memory_source_suppressions (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, message_id)
);

CREATE TABLE IF NOT EXISTS memory_conversation_controls (
    session_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'auto',
    excluded_at TEXT,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS memory_candidates (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    confidence REAL NOT NULL,
    classification TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    memory_id TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_candidates_status
    ON memory_candidates(status, created_at);

CREATE TABLE IF NOT EXISTS memory_privacy_ledger (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    watermark INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO memory_privacy_ledger(id, watermark) VALUES (1, 0);

-- A record id tombstone closes the restore gap for edited/superseded rows:
-- hashes alone cannot identify an older version that is absent from the
-- current live record.
CREATE TABLE IF NOT EXISTS memory_record_suppressions (
    memory_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
