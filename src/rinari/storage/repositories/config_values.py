import json
from collections.abc import Sequence

from rinari.storage.db import Database
from rinari.storage.records import ConfigValue


class ConfigValueRepository:
    """Key/value runtime state (active provider, profile, etc.).

    Values are always stored as strings; services own the serialization
    format of each key.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, key: str) -> str | None:
        row = self._db.query_one("SELECT value FROM config_values WHERE key = ?", (key,))
        return row["value"] if row else None

    def get_json(self, key: str) -> dict | list | None:
        raw = self.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    def set(self, value: ConfigValue) -> None:
        self._db.execute(
            """
            INSERT INTO config_values (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (value.key, value.value, value.updated_at),
        )

    def set_json(self, key: str, obj: dict | list, updated_at: str) -> None:
        self.set(ConfigValue(key=key, value=json.dumps(obj, sort_keys=True), updated_at=updated_at))

    def delete(self, key: str) -> bool:
        cursor = self._db.execute("DELETE FROM config_values WHERE key = ?", (key,))
        return cursor.rowcount > 0

    def all(self) -> dict[str, str]:
        rows = self._db.query("SELECT key, value FROM config_values ORDER BY key")
        return {row["key"]: row["value"] for row in rows}

    def keys_with_prefix(self, prefix: str) -> Sequence[str]:
        rows = self._db.query(
            "SELECT key FROM config_values WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
            (prefix.replace("%", r"\%").replace("_", r"\_") + "%",),
        )
        return [row["key"] for row in rows]
