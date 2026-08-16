import json

from rinari.storage.db import Database
from rinari.storage.records import ProviderCredentialRef, ProviderRecord


class ProviderRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def insert(self, rec: ProviderRecord) -> None:
        self._db.execute(
            """
            INSERT INTO providers (
                id, alias, type, auth_method, account_hint, endpoint,
                settings_json, default_model_id, last_used_model_id,
                status_connected, status_checked_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.id,
                rec.alias,
                rec.type,
                rec.auth_method,
                rec.account_hint,
                rec.endpoint,
                _dumps(rec.settings),
                rec.default_model_id,
                rec.last_used_model_id,
                _tri(rec.status_connected),
                rec.status_checked_at,
                rec.created_at,
                rec.updated_at,
            ),
        )

    def get(self, provider_id: str) -> ProviderRecord | None:
        row = self._db.query_one("SELECT * FROM providers WHERE id = ?", (provider_id,))
        return _row_to_record(row) if row else None

    def get_by_alias(self, alias: str) -> ProviderRecord | None:
        row = self._db.query_one("SELECT * FROM providers WHERE alias = ?", (alias,))
        return _row_to_record(row) if row else None

    def list(self) -> list[ProviderRecord]:
        rows = self._db.query("SELECT * FROM providers ORDER BY created_at, alias")
        return [_row_to_record(r) for r in rows]

    def update(self, rec: ProviderRecord) -> None:
        self._db.execute(
            """
            UPDATE providers SET
                alias = ?, type = ?, auth_method = ?, account_hint = ?, endpoint = ?,
                settings_json = ?, default_model_id = ?, last_used_model_id = ?,
                status_connected = ?, status_checked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                rec.alias,
                rec.type,
                rec.auth_method,
                rec.account_hint,
                rec.endpoint,
                _dumps(rec.settings),
                rec.default_model_id,
                rec.last_used_model_id,
                _tri(rec.status_connected),
                rec.status_checked_at,
                rec.updated_at,
                rec.id,
            ),
        )

    def delete(self, provider_id: str) -> bool:
        cursor = self._db.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
        return cursor.rowcount > 0

    def set_credential(self, ref: ProviderCredentialRef) -> None:
        self._db.execute(
            """
            INSERT INTO provider_credentials_metadata (provider_id, secret_ref, method, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(provider_id) DO UPDATE SET
                secret_ref = excluded.secret_ref,
                method = excluded.method,
                updated_at = excluded.updated_at
            """,
            (ref.provider_id, ref.secret_ref, ref.method, ref.updated_at),
        )

    def get_credential(self, provider_id: str) -> ProviderCredentialRef | None:
        row = self._db.query_one(
            "SELECT * FROM provider_credentials_metadata WHERE provider_id = ?",
            (provider_id,),
        )
        if not row:
            return None
        return ProviderCredentialRef(
            provider_id=row["provider_id"],
            secret_ref=row["secret_ref"],
            method=row["method"],
            updated_at=row["updated_at"],
        )


def _dumps(value: dict) -> str:
    return json.dumps(value, sort_keys=True)


def _loads(value: str) -> dict:
    return json.loads(value) if value else {}


def _tri(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _untri(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def _row_to_record(row: dict) -> ProviderRecord:
    return ProviderRecord(
        id=row["id"],
        alias=row["alias"],
        type=row["type"],
        auth_method=row["auth_method"],
        account_hint=row["account_hint"],
        endpoint=row["endpoint"],
        settings=_loads(row["settings_json"]),
        default_model_id=row["default_model_id"],
        last_used_model_id=row["last_used_model_id"],
        status_connected=_untri(row["status_connected"]),
        status_checked_at=row["status_checked_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
