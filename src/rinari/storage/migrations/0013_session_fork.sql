-- 0013: session fork provenance.
-- forked_from stores the source session id when this session was created by
-- `session fork`, so the lineage is durable and survives exports.
ALTER TABLE sessions ADD COLUMN forked_from TEXT;