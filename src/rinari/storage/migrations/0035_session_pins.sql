-- 0035: pinned conversations. NULL means not pinned and the timestamp orders
-- the pinned list (most recently pinned first). Written only by its own UPDATE
-- so a turn that rewrites a stale session record can never unpin it.
ALTER TABLE sessions ADD COLUMN pinned_at TEXT;
