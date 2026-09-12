-- Suppression applies to normalized remembered text even when a caller changes
-- the topic label. Keep the topic hash as minimal audit metadata.
CREATE INDEX IF NOT EXISTS idx_memory_suppressions_text_hash
    ON memory_suppressions (text_hash);
