"""Project trust repository (phase 3)."""

from rinari.storage.db import Database
from rinari.storage.records import TrustEntryRecord


class TrustEntryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, canonical_path: str) -> TrustEntryRecord | None:
        row = self._db.query_one(
            "SELECT * FROM trust_entries WHERE canonical_path = ?", (canonical_path,)
        )
        return _to_record(row) if row else None

    def upsert(self, rec: TrustEntryRecord) -> None:
        existing = self.get(rec.canonical_path)
        if existing is None:
            self._db.execute(
                """
                INSERT INTO trust_entries (canonical_path, fingerprint, trusted_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (rec.canonical_path, rec.fingerprint, rec.trusted_at, rec.updated_at),
            )
            return
        self._db.execute(
            """
            UPDATE trust_entries
            SET fingerprint = ?, trusted_at = ?, updated_at = ?
            WHERE canonical_path = ?
            """,
            (rec.fingerprint, rec.trusted_at, rec.updated_at, rec.canonical_path),
        )

    def remove(self, canonical_path: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM trust_entries WHERE canonical_path = ?", (canonical_path,)
        )
        return cursor.rowcount > 0

    def list(self) -> list[TrustEntryRecord]:
        rows = self._db.query("SELECT * FROM trust_entries ORDER BY canonical_path")
        return [_to_record(row) for row in rows]


def _to_record(row: dict) -> TrustEntryRecord:
    return TrustEntryRecord(
        canonical_path=row["canonical_path"],
        fingerprint=row["fingerprint"],
        trusted_at=row["trusted_at"],
        updated_at=row["updated_at"],
    )
