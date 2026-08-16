import json

from rinari.storage.db import Database
from rinari.storage.records import ModelRecord


class ModelRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def insert(self, rec: ModelRecord) -> None:
        self._db.execute(
            """
            INSERT INTO models (
                id, alias, provider_id, provider_model_id,
                settings_json, capabilities_json, availability, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.id,
                rec.alias,
                rec.provider_id,
                rec.provider_model_id,
                json.dumps(rec.settings, sort_keys=True),
                json.dumps(rec.capabilities, sort_keys=True)
                if rec.capabilities is not None
                else None,
                rec.availability,
                rec.created_at,
                rec.updated_at,
            ),
        )

    def get(self, model_id: str) -> ModelRecord | None:
        row = self._db.query_one("SELECT * FROM models WHERE id = ?", (model_id,))
        return _row_to_record(row) if row else None

    def get_by_alias(self, provider_id: str, alias: str) -> ModelRecord | None:
        row = self._db.query_one(
            "SELECT * FROM models WHERE provider_id = ? AND alias = ?",
            (provider_id, alias),
        )
        return _row_to_record(row) if row else None

    def get_by_provider_model_id(
        self, provider_id: str, provider_model_id: str
    ) -> ModelRecord | None:
        row = self._db.query_one(
            "SELECT * FROM models WHERE provider_id = ? AND provider_model_id = ?",
            (provider_id, provider_model_id),
        )
        return _row_to_record(row) if row else None

    def list(self, provider_id: str | None = None) -> list[ModelRecord]:
        if provider_id is None:
            rows = self._db.query("SELECT * FROM models ORDER BY updated_at DESC, alias")
        else:
            rows = self._db.query(
                "SELECT * FROM models WHERE provider_id = ? ORDER BY alias", (provider_id,)
            )
        return [_row_to_record(r) for r in rows]

    def update(self, rec: ModelRecord) -> None:
        self._db.execute(
            """
            UPDATE models SET
                alias = ?, provider_model_id = ?, settings_json = ?,
                capabilities_json = ?, availability = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                rec.alias,
                rec.provider_model_id,
                json.dumps(rec.settings, sort_keys=True),
                json.dumps(rec.capabilities, sort_keys=True)
                if rec.capabilities is not None
                else None,
                rec.availability,
                rec.updated_at,
                rec.id,
            ),
        )

    def delete(self, model_id: str) -> bool:
        cursor = self._db.execute("DELETE FROM models WHERE id = ?", (model_id,))
        return cursor.rowcount > 0

    def delete_by_provider(self, provider_id: str) -> int:
        cursor = self._db.execute("DELETE FROM models WHERE provider_id = ?", (provider_id,))
        return cursor.rowcount


def _row_to_record(row: dict) -> ModelRecord:
    capabilities = row["capabilities_json"]
    return ModelRecord(
        id=row["id"],
        alias=row["alias"],
        provider_id=row["provider_id"],
        provider_model_id=row["provider_model_id"],
        settings=json.loads(row["settings_json"]) if row["settings_json"] else {},
        capabilities=json.loads(capabilities) if capabilities else None,
        availability=row["availability"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
