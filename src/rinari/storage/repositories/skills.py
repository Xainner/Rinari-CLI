"""Skill records: provenance, on/off and approval state (migration 0036).

The skill itself stays a folder with a SKILL.md; this table only holds what
the folder cannot say about itself.
"""

from __future__ import annotations

from rinari.storage.db import Database

_COLUMNS = (
    "origin",
    "source_kind",
    "source",
    "content_hash",
    "enabled",
    "status",
    "learned_from",
    "installed_at",
    "updated_at",
)


class SkillRecordRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, name: str) -> dict | None:
        row = self._db.query_one("SELECT * FROM skill_records WHERE name = ?", (name,))
        return dict(row) if row else None

    def list(self) -> list[dict]:
        return [dict(row) for row in self._db.query("SELECT * FROM skill_records ORDER BY name")]

    def upsert(self, name: str, **fields) -> dict:
        unknown = set(fields) - set(_COLUMNS)
        if unknown:
            raise ValueError(f"unknown skill record fields: {sorted(unknown)}")
        current = self.get(name) or {}
        row = {column: fields.get(column, current.get(column)) for column in _COLUMNS}
        row["enabled"] = 1 if row["enabled"] in (None, 1, True) else 0
        row["origin"] = row["origin"] or "installed"
        row["source_kind"] = row["source_kind"] or "local"
        row["status"] = row["status"] or "active"
        self._db.execute(
            f"""
            INSERT INTO skill_records (name, {", ".join(_COLUMNS)})
            VALUES (?, {", ".join("?" for _ in _COLUMNS)})
            ON CONFLICT(name) DO UPDATE SET
                {", ".join(f"{column} = excluded.{column}" for column in _COLUMNS)}
            """,
            (name, *(row[column] for column in _COLUMNS)),
        )
        return self.get(name) or {}

    def delete(self, name: str) -> bool:
        before = self.get(name)
        self._db.execute("DELETE FROM skill_records WHERE name = ?", (name,))
        return before is not None


__all__ = ["SkillRecordRepository"]
