-- Phase 3: project-scoped repository index (files, symbols, references,
-- test mapping, meta). Project-scoped by design (harness.md 98): the
-- canonical project root is part of every primary key.

CREATE TABLE IF NOT EXISTS repo_index_files (
    project_root TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    hash TEXT NOT NULL,
    language TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    symbols_json TEXT NOT NULL,
    imports_json TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (project_root, rel_path)
);

CREATE TABLE IF NOT EXISTS repo_index_symbols (
    project_root TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    PRIMARY KEY (project_root, qualified_name, rel_path, line)
);

CREATE INDEX IF NOT EXISTS idx_repo_index_symbols_name
    ON repo_index_symbols(project_root, name);

CREATE TABLE IF NOT EXISTS repo_index_references (
    project_root TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    PRIMARY KEY (project_root, symbol, rel_path, line)
);

CREATE INDEX IF NOT EXISTS idx_repo_index_refs_symbol
    ON repo_index_references(project_root, symbol);

CREATE TABLE IF NOT EXISTS repo_index_test_map (
    project_root TEXT NOT NULL,
    test_file TEXT NOT NULL,
    target_file TEXT NOT NULL,
    PRIMARY KEY (project_root, test_file, target_file)
);

CREATE TABLE IF NOT EXISTS repo_index_meta (
    project_root TEXT PRIMARY KEY,
    built_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    files INTEGER NOT NULL,
    symbols INTEGER NOT NULL,
    ref_count INTEGER NOT NULL,
    semantic_layer TEXT NOT NULL
);