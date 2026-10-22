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
    "reasoning_effort",
    "icon",
    "permission_profile",
    "connectors_json",
    "budget_json",
    "ask_new_destinations",
    "soul_id",
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
            "reasoning_effort": agent.get("reasoning_effort"),
            "icon": agent.get("icon"),
            "permission_profile": agent.get("permission_profile") or "workspace",
            "connectors_json": json.dumps(list(agent.get("connectors") or [])),
            "budget_json": json.dumps(dict(agent.get("budget") or {}), sort_keys=True),
            "ask_new_destinations": None if ask is None else int(bool(ask)),
            "soul_id": agent.get("soul_id"),
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

    def delete(self, agent_id: str) -> None:
        """The agent and its posts in any team (an archived team keeps its posts)."""
        with self._db.transaction():
            self._db.execute("DELETE FROM agency_posts WHERE agent_id = ?", (agent_id,))
            self._db.execute("UPDATE sessions SET agent_id = NULL WHERE agent_id = ?", (agent_id,))
            self._db.execute("DELETE FROM agency_agents WHERE id = ?", (agent_id,))

    def set_session_agent(self, session_id: str, agent_id: str | None) -> None:
        self._db.execute("UPDATE sessions SET agent_id = ? WHERE id = ?", (agent_id, session_id))


# -- A2: teams, posts, the office chat and errands (migration 0063) --------------

_TEAM_COLUMNS = (
    "rinari_profile_id",
    "name",
    "purpose",
    "chief_agent_id",
    "connectors_json",
    "budget_json",
    "created_at",
    "updated_at",
    "archived_at",
)
_ERRAND_COLUMNS = (
    "team_id",
    "from_team_id",
    "from_agent_id",
    "to_agent_id",
    "title",
    "body",
    "done_when_json",
    "status",
    "result_json",
    "return_reason",
    "chain_id",
    "hop",
    "production_run_id",
    "session_id",
    "turn_id",
    "created_at",
    "updated_at",
    "started_at",
    "delivered_at",
    "deliveries",
    "returns",
    "external",
    "halted_json",
)


def _team(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["connectors"] = json.loads(item.pop("connectors_json") or "[]")
    item["budget"] = json.loads(item.pop("budget_json") or "{}")
    return item


def _errand(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["done_when"] = json.loads(item.pop("done_when_json") or "[]")
    raw = item.pop("result_json", None)
    item["result"] = json.loads(raw) if raw else None
    item["external"] = bool(item.get("external"))
    item["deliveries"] = int(item.get("deliveries") or 0)
    item["returns"] = int(item.get("returns") or 0)
    halted = item.pop("halted_json", None)
    item["halted"] = json.loads(halted) if halted else None
    return item


def _office(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["mentions"] = json.loads(item.pop("mentions_json") or "[]")
    raw = item.pop("notice_json", None)
    item["notice"] = json.loads(raw) if raw else None
    item["system"] = bool(item.get("system"))
    item["external"] = bool(item.get("external"))
    return item


class TeamRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- teams ---------------------------------------------------------------

    def get(self, team_id: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM agency_teams WHERE id = ?", (team_id,))
        return _team(row) if row else None

    def list(
        self, rinari_profile_id: str | None = None, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agency_teams WHERE 1=1"
        params: list[object] = []
        if rinari_profile_id is not None:
            sql += " AND rinari_profile_id = ?"
            params.append(rinari_profile_id)
        if not include_archived:
            sql += " AND archived_at IS NULL"
        sql += " ORDER BY created_at"
        return [_team(row) for row in self._db.query(sql, params)]

    def save(self, team_id: str, team: dict[str, Any]) -> dict[str, Any]:
        row = {
            "rinari_profile_id": team.get("rinari_profile_id") or "default",
            "name": team["name"],
            "purpose": team.get("purpose") or "",
            "chief_agent_id": team.get("chief_agent_id"),
            "connectors_json": json.dumps(list(team.get("connectors") or [])),
            "budget_json": json.dumps(dict(team.get("budget") or {}), sort_keys=True),
            "created_at": team["created_at"],
            "updated_at": team["updated_at"],
            "archived_at": team.get("archived_at"),
        }
        self._db.execute(
            f"""
            INSERT INTO agency_teams (id, {", ".join(_TEAM_COLUMNS)})
            VALUES (?, {", ".join("?" for _ in _TEAM_COLUMNS)})
            ON CONFLICT(id) DO UPDATE SET
                {", ".join(f"{column} = excluded.{column}" for column in _TEAM_COLUMNS)}
            """,
            (team_id, *(row[column] for column in _TEAM_COLUMNS)),
        )
        return self.get(team_id) or {}

    def delete(self, team_id: str) -> None:
        """The team and what is only its: posts, office, errands and waits."""
        posts = [row["session_id"] for row in self.posts(team_id)]
        with self._db.transaction():
            for session_id in posts:
                self._db.execute("DELETE FROM agency_waits WHERE session_id = ?", (session_id,))
            self._db.execute("DELETE FROM agency_waits WHERE team_id = ?", (team_id,))
            self._db.execute("DELETE FROM agency_errands WHERE team_id = ?", (team_id,))
            self._db.execute("DELETE FROM agency_office_messages WHERE team_id = ?", (team_id,))
            self._db.execute("DELETE FROM agency_posts WHERE team_id = ?", (team_id,))
            self._db.execute("UPDATE sessions SET team_id = NULL WHERE team_id = ?", (team_id,))
            self._db.execute("DELETE FROM agency_teams WHERE id = ?", (team_id,))

    # -- posts ---------------------------------------------------------------

    def posts(self, team_id: str) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM agency_posts WHERE team_id = ? ORDER BY created_at", (team_id,)
        )
        return [dict(row) for row in rows]

    def post(self, team_id: str, agent_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT * FROM agency_posts WHERE team_id = ? AND agent_id = ?", (team_id, agent_id)
        )
        return dict(row) if row else None

    def post_for_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM agency_posts WHERE session_id = ?", (session_id,))
        return dict(row) if row else None

    def posts_of_agent(self, agent_id: str) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM agency_posts WHERE agent_id = ?", (agent_id,))
        return [dict(row) for row in rows]

    def add_post(self, team_id: str, agent_id: str, session_id: str, now: float) -> None:
        self._db.execute(
            "INSERT INTO agency_posts (team_id, agent_id, session_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (team_id, agent_id, session_id, now),
        )
        self._db.execute(
            "UPDATE sessions SET team_id = ?, agent_id = ? WHERE id = ?",
            (team_id, agent_id, session_id),
        )

    def set_session_team(self, session_id: str, team_id: str | None) -> None:
        """A child or fork of a post works for the same team."""
        self._db.execute("UPDATE sessions SET team_id = ? WHERE id = ?", (team_id, session_id))

    def active_team(self, record: Any) -> str | None:
        """The team a conversation works for: while its agent is still in that team."""
        team_id = getattr(record, "team_id", None)
        agent_id = getattr(record, "agent_id", None)
        if not team_id or not agent_id or self.post(team_id, agent_id) is None:
            return None
        return team_id

    def remove_post(self, team_id: str, agent_id: str) -> None:
        self._db.execute(
            "DELETE FROM agency_posts WHERE team_id = ? AND agent_id = ?", (team_id, agent_id)
        )

    def team_session_ids(self, team_id: str) -> list[str]:
        rows = self._db.query("SELECT id FROM sessions WHERE team_id = ?", (team_id,))
        return [row["id"] for row in rows]

    # -- the office chat -------------------------------------------------------

    def add_message(self, message: dict[str, Any]) -> dict[str, Any]:
        self._db.execute(
            """
            INSERT INTO agency_office_messages (
                id, team_id, author_kind, agent_id, text, mentions_json, errand_id,
                chain_id, hop, created_at, system, notice_json, external
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message["id"],
                message["team_id"],
                message["author_kind"],
                message.get("agent_id"),
                message["text"],
                json.dumps(list(message.get("mentions") or [])),
                message.get("errand_id"),
                message["chain_id"],
                int(message.get("hop") or 0),
                message["created_at"],
                int(bool(message.get("system"))),
                json.dumps(message["notice"], sort_keys=True) if message.get("notice") else None,
                int(bool(message.get("external"))),
            ),
        )
        return self.message(message["id"]) or {}

    def message(self, message_id: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM agency_office_messages WHERE id = ?", (message_id,))
        return _office(row) if row else None

    def messages(self, team_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM (SELECT * FROM agency_office_messages WHERE team_id = ? "
            "ORDER BY created_at DESC LIMIT ?) ORDER BY created_at",
            (team_id, limit),
        )
        return [_office(row) for row in rows]

    def messages_before(
        self, team_id: str, before: float | None, limit: int
    ) -> list[dict[str, Any]]:
        """The `limit` messages before `before` (the newest ones without it), oldest first."""
        rows = self._db.query(
            "SELECT * FROM (SELECT * FROM agency_office_messages WHERE team_id = ? "
            "AND (? IS NULL OR created_at < ?) ORDER BY created_at DESC LIMIT ?) "
            "ORDER BY created_at",
            (team_id, before, before, limit),
        )
        return [_office(row) for row in rows]

    def set_notice(self, message_id: str, notice: dict[str, Any] | None) -> None:
        """Change what an Engine notice says (a stopped chain the owner dismissed)."""
        self._db.execute(
            "UPDATE agency_office_messages SET notice_json = ? WHERE id = ?",
            (json.dumps(notice, sort_keys=True) if notice else None, message_id),
        )

    def stop_notices(
        self, team_ids: list[str] | None = None, *, open_only: bool = False
    ) -> list[dict[str, Any]]:
        """The office notices of stopped chains, oldest first; open_only: not dismissed."""
        sql = (
            "SELECT * FROM agency_office_messages WHERE system = 1 "
            'AND notice_json LIKE \'%"kind": "stopped"%\''
        )
        params: list[object] = []
        if team_ids is not None:
            if not team_ids:
                return []
            sql += f" AND team_id IN ({', '.join('?' for _ in team_ids)})"
            params.extend(team_ids)
        sql += " ORDER BY created_at"
        rows = [_office(row) for row in self._db.query(sql, params)]
        rows = [row for row in rows if (row.get("notice") or {}).get("kind") == "stopped"]
        if open_only:
            rows = [row for row in rows if not row["notice"].get("dismissed_at")]
        return rows

    def chain_count(self, chain_id: str) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM agency_office_messages WHERE chain_id = ?", (chain_id,)
        )
        return int(row["n"]) if row else 0

    # -- errands ---------------------------------------------------------------

    def errand(self, errand_id: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM agency_errands WHERE id = ?", (errand_id,))
        return _errand(row) if row else None

    def errands(
        self, team_id: str | None = None, *, statuses: tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agency_errands WHERE 1=1"
        params: list[object] = []
        if team_id is not None:
            sql += " AND team_id = ?"
            params.append(team_id)
        if statuses:
            sql += f" AND status IN ({', '.join('?' for _ in statuses)})"
            params.extend(statuses)
        sql += " ORDER BY created_at"
        return [_errand(row) for row in self._db.query(sql, params)]

    def save_errand(self, errand_id: str, errand: dict[str, Any]) -> dict[str, Any]:
        result = errand.get("result")
        row = {
            **{column: errand.get(column) for column in _ERRAND_COLUMNS},
            "done_when_json": json.dumps(list(errand.get("done_when") or [])),
            "result_json": json.dumps(result, sort_keys=True) if result is not None else None,
            "hop": int(errand.get("hop") or 0),
            "status": errand.get("status") or "pending",
            "body": errand.get("body") or "",
            "deliveries": int(errand.get("deliveries") or 0),
            "returns": int(errand.get("returns") or 0),
            "external": int(bool(errand.get("external"))),
            "halted_json": (
                json.dumps(errand["halted"], sort_keys=True) if errand.get("halted") else None
            ),
        }
        self._db.execute(
            f"""
            INSERT INTO agency_errands (id, {", ".join(_ERRAND_COLUMNS)})
            VALUES (?, {", ".join("?" for _ in _ERRAND_COLUMNS)})
            ON CONFLICT(id) DO UPDATE SET
                {", ".join(f"{column} = excluded.{column}" for column in _ERRAND_COLUMNS)}
            """,
            (errand_id, *(row[column] for column in _ERRAND_COLUMNS)),
        )
        return self.errand(errand_id) or {}

    # -- the posts' queues (replan 22-oct) ------------------------------------------

    def queues(self) -> dict[str, list[dict[str, Any]]]:
        rows = self._db.query("SELECT session_id, items_json FROM agency_queue")
        return {row["session_id"]: json.loads(row["items_json"]) for row in rows}

    def save_queues(self, queues: dict[str, list[dict[str, Any]]], now: float) -> None:
        """The queues as they are now: an empty queue is not kept."""
        with self._db.transaction():
            self._db.execute("DELETE FROM agency_queue")
            for session_id, items in queues.items():
                if items:
                    self._db.execute(
                        "INSERT INTO agency_queue (session_id, items_json, updated_at) "
                        "VALUES (?, ?, ?)",
                        (session_id, json.dumps(items), now),
                    )

    # -- waits (office.wait) ---------------------------------------------------

    def waits(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._db.query("SELECT * FROM agency_waits ORDER BY since")]

    def save_wait(self, wait: dict[str, Any]) -> None:
        self._db.execute(
            """
            INSERT INTO agency_waits (
                session_id, team_id, agent_id, for_agent_id, chain_id, hop, message_at, since
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                team_id = excluded.team_id, agent_id = excluded.agent_id,
                for_agent_id = excluded.for_agent_id, chain_id = excluded.chain_id,
                hop = excluded.hop, message_at = excluded.message_at, since = excluded.since
            """,
            (
                wait["session_id"],
                wait["team_id"],
                wait["agent_id"],
                wait["for_agent_id"],
                wait["chain_id"],
                int(wait.get("hop") or 0),
                float(wait.get("message_at") or 0),
                wait["since"],
            ),
        )

    def drop_wait(self, session_id: str) -> None:
        self._db.execute("DELETE FROM agency_waits WHERE session_id = ?", (session_id,))

    def drop_waits(self) -> None:
        self._db.execute("DELETE FROM agency_waits")
