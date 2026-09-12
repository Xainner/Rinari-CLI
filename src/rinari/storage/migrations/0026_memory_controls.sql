-- Personal memory controls: optimistic revision and durable minimal suppression.
-- Suppression stores only hashes so forgetting does not retain the remembered value.
ALTER TABLE user_memory ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS memory_suppressions (
    topic_hash TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (topic_hash, text_hash)
);
