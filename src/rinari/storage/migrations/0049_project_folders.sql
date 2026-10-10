-- A project can have several working folders. Position 0 is the primary one
-- (projects.canonical_root, which still keys memory, tasks, checkpoints and
-- grants). A folder belongs to one project only. Every existing project gets
-- its root as the primary folder. NOTE: no semicolons in comments.
CREATE TABLE IF NOT EXISTS project_folders (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  canonical_path TEXT NOT NULL,
  position INTEGER NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  added_at TEXT NOT NULL,
  PRIMARY KEY (project_id, canonical_path)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_folders_path ON project_folders(canonical_path);
INSERT OR IGNORE INTO project_folders (project_id, canonical_path, position, label, added_at)
SELECT id, canonical_root, 0, '', created_at FROM projects;
