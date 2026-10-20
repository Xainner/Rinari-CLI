"""Rinari Agency agents (migration 0062).

Rows only: validation and the meaning of each field live in
`rinari.agency.service`.
"""

from __future__ import annotations

import json
from typing import Any

from rinari.storage.db import Database

_COLUMNS = (
    "rinari_profile_id",
    "name",
    "role",
    "instructions",
    "model",
    "permission_profile",
    "connectors_json",
    "budget_json",
    "ask_new_destinations",
    "session_id",
    "created_at",
    "updated_at",
    "archived_at",
)


def _agent(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["connectors"] = json.loads(item.pop("connectors_json") or "[]")
    item["budget"] = json.loads(item.pop("budget_json") or "{}")
    ask = item.get("ask_new_destinations")
    item["ask_new_destinations"] = None if ask is None else bool(ask)
    return item


class AgencyRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, agent_id: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM agency_agents WHERE id = ?", (agent_id,))
        return _agent(row) if row else None

    def list(
        self, rinari_profile_id: str | None = None, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agency_agents WHERE 1=1"
        params: list[object] = []
        if rinari_profile_id is not None:
            sql += " AND rinari_profile_id = ?"
            params.append(rinari_profile_id)
        if not include_archived:
            sql += " AND archived_at IS NULL"
        sql += " ORDER BY created_at"
        return [_agent(row) for row in self._db.query(sql, params)]

    def for_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT a.* FROM agency_agents a JOIN sessions s ON s.agent_id = a.id WHERE s.id = ?",
            (session_id,),
        )
        return _agent(row) if row else None

    def save(self, agent_id: str, agent: dict[str, Any]) -> dict[str, Any]:
        ask = agent.get("ask_new_destinations")
        row = {
            "rinari_profile_id": agent.get("rinari_profile_id") or "default",
            "name": agent["name"],
            "role": agent.get("role") or "",
            "instructions": agent.get("instructions") or "",
            "model": agent.get("model"),
            "permission_profile": agent.get("permission_profile") or "workspace",
            "connectors_json": json.dumps(list(agent.get("connectors") or [])),
            "budget_json": json.dumps(dict(agent.get("budget") or {}), sort_keys=True),
            "ask_new_destinations": None if ask is None else int(bool(ask)),
            "session_id": agent.get("session_id"),
            "created_at": agent["created_at"],
            "updated_at": agent["updated_at"],
            "archived_at": agent.get("archived_at"),
        }
        self._db.execute(
            f"""
            INSERT INTO agency_agents (id, {", ".join(_COLUMNS)})
            VALUES (?, {", ".join("?" for _ in _COLUMNS)})
            ON CONFLICT(id) DO UPDATE SET
                {", ".join(f"{column} = excluded.{column}" for column in _COLUMNS)}
            """,
            (agent_id, *(row[column] for column in _COLUMNS)),
        )
        return self.get(agent_id) or {}

    def session_ids(self, agent_id: str) -> list[str]:
        rows = self._db.query("SELECT id FROM sessions WHERE agent_id = ?", (agent_id,))
        return [row["id"] for row in rows]

    def set_session_agent(self, session_id: str, agent_id: str | None) -> None:
        self._db.execute("UPDATE sessions SET agent_id = ? WHERE id = ?", (agent_id, session_id))
