-- Per-session Soul override (session scope). NULL inherits the global
-- active Soul through the Soul 3.0 chain (active custom, legacy
-- ~/soul.md, bundled default). Unknown ids never persist: the service
-- validates against the SoulStore before writing.
ALTER TABLE sessions ADD COLUMN soul_id TEXT NULL;
