"""Repository index repository (phase 3)."""

from rinari.storage.db import Database


class IndexRepository:
    """Project-scoped store for files/symbols/references/test-map/meta.

    Every keyed row carries the canonical project root, so one Rinari state
    database can index many projects (harness.md 98).
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def transaction(self):
        return self._db.transaction()

    # -- files ----------------------------------------------------------------

    def index_files(self, project_root: str) -> dict[str, dict]:
        rows = self._db.query(
            "SELECT * FROM repo_index_files WHERE project_root = ?", (project_root,)
        )
        return {row["rel_path"]: dict(row) for row in rows}

    def replace_index_files(self, db: Database, project_root: str, rows: list[dict]) -> None:
        db.execute("DELETE FROM repo_index_files WHERE project_root = ?", (project_root,))
        db.executemany(
            """
            INSERT INTO repo_index_files
                (project_root, rel_path, hash, language, size_bytes,
                 symbols_json, imports_json, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    project_root,
                    row["rel_path"],
                    row["hash"],
                    row["language"],
                    row["size_bytes"],
                    row["symbols_json"],
                    row["imports_json"],
                    row["indexed_at"],
                )
                for row in rows
            ],
        )

    # -- symbols ---------------------------------------------------------------

    def replace_symbols(self, db: Database, project_root: str, rows: list[dict]) -> None:
        db.execute("DELETE FROM repo_index_symbols WHERE project_root = ?", (project_root,))
        db.executemany(
            """
            INSERT INTO repo_index_symbols
                (project_root, name, kind, qualified_name, rel_path, line)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    project_root,
                    row["name"],
                    row["kind"],
                    row["qualified_name"],
                    row["rel_path"],
                    row["line"],
                )
                for row in rows
            ],
        )

    def symbols_all(self, project_root: str, *, limit: int = 2000) -> list[dict]:
        rows = self._db.query(
            """
            SELECT name, kind, qualified_name, rel_path, line
            FROM repo_index_symbols
            WHERE project_root = ?
            ORDER BY rel_path, line
            LIMIT ?
            """,
            (project_root, limit),
        )
        return [dict(row) for row in rows]

    def symbols_by_name(self, project_root: str, name: str, *, limit: int = 50) -> list[dict]:
        rows = self._db.query(
            """
            SELECT name, kind, qualified_name, rel_path, line
            FROM repo_index_symbols
            WHERE project_root = ? AND name = ?
            ORDER BY rel_path, line
            LIMIT ?
            """,
            (project_root, name, limit),
        )
        return [dict(row) for row in rows]

    # -- references --------------------------------------------------------------

    def replace_references(self, db: Database, project_root: str, rows: list[dict]) -> None:
        db.execute("DELETE FROM repo_index_references WHERE project_root = ?", (project_root,))
        db.executemany(
            """
            INSERT INTO repo_index_references (project_root, symbol, rel_path, line)
            VALUES (?, ?, ?, ?)
            """,
            [(project_root, row["symbol"], row["rel_path"], row["line"]) for row in rows],
        )

    def references(self, project_root: str, symbol: str, *, limit: int = 50) -> list[dict]:
        rows = self._db.query(
            """
            SELECT symbol, rel_path, line
            FROM repo_index_references
            WHERE project_root = ? AND symbol = ?
            ORDER BY rel_path, line
            LIMIT ?
            """,
            (project_root, symbol, limit),
        )
        return [dict(row) for row in rows]

    # -- test mapping --------------------------------------------------------------

    def replace_test_map(self, db: Database, project_root: str, rows: list[dict]) -> None:
        db.execute("DELETE FROM repo_index_test_map WHERE project_root = ?", (project_root,))
        db.executemany(
            """
            INSERT INTO repo_index_test_map (project_root, test_file, target_file)
            VALUES (?, ?, ?)
            """,
            [(project_root, row["test_file"], row["target_file"]) for row in rows],
        )

    def test_map(self, project_root: str) -> list[dict]:
        rows = self._db.query(
            "SELECT test_file, target_file FROM repo_index_test_map "
            "WHERE project_root = ? ORDER BY target_file, test_file",
            (project_root,),
        )
        return [dict(row) for row in rows]

    def tests_touching(self, project_root: str, files: set[str]) -> list[str]:
        if not files:
            return []
        marks = ", ".join("?" for _ in files)
        rows = self._db.query(
            f"""
            SELECT DISTINCT test_file
            FROM repo_index_test_map
            WHERE project_root = ? AND target_file IN ({marks})
            ORDER BY test_file
            """,
            (project_root, *sorted(files)),
        )
        return [row["test_file"] for row in rows]

    # -- meta ------------------------------------------------------------------------

    def set_meta(
        self,
        db: Database,
        project_root: str,
        built_at: str,
        updated_at: str,
        meta: dict,
    ) -> None:
        db.execute(
            """
            INSERT INTO repo_index_meta
                (project_root, built_at, updated_at, files, symbols, ref_count, semantic_layer)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_root) DO UPDATE SET
                built_at = excluded.built_at,
                updated_at = excluded.updated_at,
                files = excluded.files,
                symbols = excluded.symbols,
                ref_count = excluded.ref_count,
                semantic_layer = excluded.semantic_layer
            """,
            (
                project_root,
                built_at,
                updated_at,
                meta["files"],
                meta["symbols"],
                meta["references"],
                meta["semantic_layer"],
            ),
        )

    def get_meta(self, project_root: str) -> dict | None:
        row = self._db.query_one(
            "SELECT * FROM repo_index_meta WHERE project_root = ?", (project_root,)
        )
        return dict(row) if row else None

    # -- lifecycle ------------------------------------------------------------------

    def clear(self, project_root: str) -> bool:
        """Delete all index state for a project. Returns True if state existed."""
        existed = self.get_meta(project_root) is not None or bool(self.index_files(project_root))
        with self._db.transaction():
            for table in (
                "repo_index_files",
                "repo_index_symbols",
                "repo_index_references",
                "repo_index_test_map",
                "repo_index_meta",
            ):
                self._db.execute(f"DELETE FROM {table} WHERE project_root = ?", (project_root,))
        return existed
