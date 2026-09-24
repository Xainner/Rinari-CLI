-- 0036: what the Engine knows about a skill beyond its folder. Where it came
-- from (provenance and content hash, to tell a local edit from an update),
-- whether the owner turned it off, and later whether it is still waiting for
-- approval (learned skills). Keyed by the resolved skill name, so turning off
-- a packaged skill needs no copy of it.
CREATE TABLE skill_records (
    name TEXT PRIMARY KEY,
    origin TEXT NOT NULL DEFAULT 'installed',
    source_kind TEXT NOT NULL DEFAULT 'local',
    source TEXT,
    content_hash TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    learned_from TEXT,
    installed_at TEXT,
    updated_at TEXT
);
