import json

from rinari.storage.db import Database
from rinari.storage.records import ProjectRecord


class ProjectRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def insert(self, rec: ProjectRecord) -> None:
        self._db.execute(
            """
            INSERT INTO projects (
                id, canonical_root, git_fingerprint, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rec.id,
                rec.canonical_root,
                rec.git_fingerprint,
                json.dumps(rec.metadata, sort_keys=True),
                rec.created_at,
                rec.updated_at,
            ),
        )

    def get(self, project_id: str) -> ProjectRecord | None:
        row = self._db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        return _row_to_record(row) if row else None

    def get_by_root(self, canonical_root: str) -> ProjectRecord | None:
        row = self._db.query_one(
            "SELECT * FROM projects WHERE canonical_root = ?", (canonical_root,)
        )
        return _row_to_record(row) if row else None

    def list(self) -> list[ProjectRecord]:
        rows = self._db.query("SELECT * FROM projects ORDER BY created_at")
        return [_row_to_record(r) for r in rows]

    def update(self, rec: ProjectRecord) -> None:
        self._db.execute(
            """
            UPDATE projects SET git_fingerprint = ?, metadata_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                rec.git_fingerprint,
                json.dumps(rec.metadata, sort_keys=True),
                rec.updated_at,
                rec.id,
            ),
        )


def _row_to_record(row: dict) -> ProjectRecord:
    return ProjectRecord(
        id=row["id"],
        canonical_root=row["canonical_root"],
        git_fingerprint=row["git_fingerprint"],
        metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
