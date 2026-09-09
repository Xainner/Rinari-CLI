-- 0022: first-class desktop project metadata and lifecycle.
ALTER TABLE projects ADD COLUMN name TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN description TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN last_opened_at TEXT;

UPDATE projects
SET name = canonical_root,
    last_opened_at = updated_at
WHERE name = '' OR last_opened_at IS NULL;
