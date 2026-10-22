"""Rinari Agency A2: teams, their posts and their errands.

A team is an office: its chat, its board of errands, its memory, its
accounts, its budget and its chief. An agent works in a team through a post
(a conversation marked with the agent and the team). Teams, posts, the chief
and the team's connectors are the owner's to set: the model never creates a
team or joins one.

Errands always start explicit (D-3) and the board is the queue: one errand at
a time per post, in order (the first way, D-28).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rinari.agency.service import EDITABLE, AgencyError, _text, normalize_budget

MAX_TEAM_NAME = 60
MAX_PURPOSE = 2000
MAX_MEMBERS = 8
MAX_ERRAND_TITLE = 200
MAX_ERRAND_BODY = 8000
MAX_DONE_WHEN = 10
MAX_DONE_ITEM = 300
# blocked: it could not start or its turn failed. It waits on the board with
# why until the owner retries it, so a failing errand never loops.
ERRAND_STATUSES = (
    "pending",
    "in_progress",
    "delivered",
    "reviewed",
    "returned",
    "blocked",
    "cancelled",
)
OPEN_STATUSES = ("pending", "in_progress", "returned", "blocked")
#: What a free post takes from the board, in order.
READY_STATUSES = ("pending", "returned")
#: Times an errand can be sent back; the next one blocks it (D-2).
MAX_RETURNS = 3

TEAM_EDITABLE = ("name", "purpose", "chief_agent_id", "connectors", "budget")
#: Where an agent's rule can deliver its result besides its run.
DELIVERY_KINDS = ("office", "agent")


def normalize_done_when(raw: Any) -> list[str]:
    """How to know an errand is done: a short list of checks (D-26)."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or len(raw) > MAX_DONE_WHEN:
        raise AgencyError(f"done_when must be a list of up to {MAX_DONE_WHEN} checks.")
    checks: list[str] = []
    for item in raw:
        text = _text(item, "A check", MAX_DONE_ITEM, required=True)
        if text not in checks:
            checks.append(text)
    return checks


class TeamService:
    def __init__(
        self,
        repo: Any,
        agents: Any,
        clock: Any,
        ids: Any,
        *,
        connector_exists: Callable[[str], bool] = lambda name: True,
    ) -> None:
        self._repo = repo
        self._agents = agents
        self._clock = clock
        self._ids = ids
        self._connector_exists = connector_exists
        # Set by the Engine server: opens a post's conversation (returns its
        # session id), announces a change (agency.team.changed) and tells the
        # owner that something stopped (agency.chain.stopped).
        self.open_post: Callable[[dict[str, Any], dict[str, Any]], str] | None = None
        self.on_changed: Callable[[str, str], None] | None = None
        self.on_stopped: Callable[[str, str, str], None] | None = None

    # -- teams ---------------------------------------------------------------------

    def get(self, team_id: Any) -> dict[str, Any]:
        if not isinstance(team_id, str) or not team_id:
            raise AgencyError("A team id is needed.")
        team = self._repo.get(team_id)
        if team is None:
            raise LookupError(f"Unknown team: {team_id}")
        return team

    def list(self, rinari_profile_id: str | None, *, include_archived: bool = False) -> list:
        return self._repo.list(rinari_profile_id, include_archived=include_archived)

    def view(self, team: dict[str, Any]) -> dict[str, Any]:
        posts = self._repo.posts(team["id"])
        return {**team, "members": [post["agent_id"] for post in posts], "posts": posts}

    def _member_card(self, agent_id: Any, profile: str) -> dict[str, Any]:
        agent = self._agents.get(agent_id)
        if agent.get("archived_at") is not None:
            raise AgencyError(f"{agent['name']} is archived.")
        if agent.get("rinari_profile_id") != profile:
            raise AgencyError(f"{agent['name']} belongs to another profile.")
        return agent

    def _normalize(self, raw: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
        unknown = set(raw) - set(TEAM_EDITABLE) - {"members"}
        if unknown:
            raise AgencyError(f"Unknown fields: {', '.join(sorted(unknown))}.")
        merged = {**(current or {}), **raw}
        connectors = merged.get("connectors") or []
        if not isinstance(connectors, list) or not all(isinstance(c, str) for c in connectors):
            raise AgencyError("connectors must be a list of connector names.")
        seen: list[str] = []
        for name in connectors:
            if name in seen:
                continue
            if not self._connector_exists(name):
                raise AgencyError(f"Unknown connector: {name}.")
            seen.append(name)
        return {
            "name": _text(merged.get("name"), "The name", MAX_TEAM_NAME, required=True),
            "purpose": _text(merged.get("purpose"), "The purpose", MAX_PURPOSE),
            "chief_agent_id": merged.get("chief_agent_id") or None,
            "connectors": seen,
            "budget": normalize_budget(merged.get("budget")),
        }

    def create(self, raw: dict[str, Any], rinari_profile_id: str = "default") -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AgencyError("A team card is needed.")
        team = self._normalize(raw, None)
        members = raw.get("members") or []
        if not isinstance(members, list) or len(members) > MAX_MEMBERS:
            raise AgencyError(f"A team has at most {MAX_MEMBERS} agents.")
        cards = [
            self._member_card(agent_id, rinari_profile_id) for agent_id in dict.fromkeys(members)
        ]
        names = [card["name"].lower() for card in cards]
        if len(set(names)) != len(names):
            raise AgencyError("Two agents of a team cannot share a name (@mentions use it).")
        chief = team["chief_agent_id"]
        if chief is None and cards:
            # By default, the first agent that joins (D-8).
            chief = cards[0]["id"]
        if chief is not None and chief not in [card["id"] for card in cards]:
            raise AgencyError("The chief must be one of the team's agents.")
        now = self._clock.now()
        team_id = self._ids.new("team")
        saved = self._repo.save(
            team_id,
            {
                **team,
                "chief_agent_id": chief,
                "rinari_profile_id": rinari_profile_id,
                "created_at": now,
                "updated_at": now,
            },
        )
        for card in cards:
            self._add_post(saved, card)
        self._changed(team_id, "created")
        return self.view(self._repo.get(team_id) or saved)

    def update(self, team_id: Any, raw: dict[str, Any]) -> dict[str, Any]:
        current = self.get(team_id)
        if current.get("archived_at") is not None:
            raise AgencyError("An archived team cannot be changed.")
        if not isinstance(raw, dict) or "members" in raw:
            raise AgencyError("Change members with join and leave.")
        team = self._normalize(raw, current)
        members = [post["agent_id"] for post in self._repo.posts(current["id"])]
        if team["chief_agent_id"] is not None and team["chief_agent_id"] not in members:
            raise AgencyError("The chief must be one of the team's agents.")
        saved = self._repo.save(current["id"], {**current, **team, "updated_at": self._clock.now()})
        self._assign_orphans(saved)
        self._changed(current["id"], "updated")
        return self.view(saved)

    def archive(self, team_id: Any) -> dict[str, Any]:
        current = self.get(team_id)
        if current.get("archived_at") is None:
            now = self._clock.now()
            for errand in self._repo.errands(current["id"], statuses=OPEN_STATUSES):
                self._repo.save_errand(
                    errand["id"],
                    {
                        **errand,
                        "status": "cancelled",
                        "return_reason": "team archived",
                        "updated_at": now,
                    },
                )
            current = self._repo.save(
                current["id"], {**current, "archived_at": now, "updated_at": now}
            )
            self._changed(current["id"], "archived")
        return self.view(current)

    def unarchive(self, team_id: Any) -> dict[str, Any]:
        """Back from the archive, with its members and posts as they were."""
        current = self.get(team_id)
        if current.get("archived_at") is not None:
            current = self._repo.save(
                current["id"], {**current, "archived_at": None, "updated_at": self._clock.now()}
            )
            self._changed(current["id"], "unarchived")
        return self.view(current)

    # -- posts -----------------------------------------------------------------------

    def _add_post(self, team: dict[str, Any], agent: dict[str, Any]) -> None:
        if self.open_post is None:
            raise AgencyError("Posts need the Engine.")
        session_id = self.open_post(team, agent)
        self._repo.add_post(team["id"], agent["id"], session_id, self._clock.now())

    def join(self, team_id: Any, agent_id: Any) -> dict[str, Any]:
        team = self.get(team_id)
        if team.get("archived_at") is not None:
            raise AgencyError("An archived team cannot change.")
        agent = self._member_card(agent_id, team["rinari_profile_id"])
        posts = self._repo.posts(team["id"])
        if any(post["agent_id"] == agent["id"] for post in posts):
            return self.view(team)
        taken = {self._agents.get(post["agent_id"])["name"].lower() for post in posts}
        if agent["name"].lower() in taken:
            raise AgencyError("Two agents of a team cannot share a name (@mentions use it).")
        if len(posts) >= MAX_MEMBERS:
            # 6.2: past 8 agents, another team (another floor).
            raise AgencyError(
                f"A team has at most {MAX_MEMBERS} agents: create another team for more."
            )
        self._add_post(team, agent)
        if team.get("chief_agent_id") is None:
            team = self._repo.save(
                team["id"], {**team, "chief_agent_id": agent["id"], "updated_at": self._clock.now()}
            )
            self._assign_orphans(team)
        self._changed(team["id"], "updated")
        return self.view(team)

    def _assign_orphans(self, team: dict[str, Any]) -> None:
        """Errands left to nobody go to the chief as soon as there is one (D-8)."""
        chief = team.get("chief_agent_id")
        if chief is None:
            return
        now = self._clock.now()
        for errand in self._repo.errands(team["id"], statuses=OPEN_STATUSES):
            if errand.get("to_agent_id") is None and errand["status"] != "in_progress":
                self._repo.save_errand(
                    errand["id"], {**errand, "to_agent_id": chief, "updated_at": now}
                )

    def leave(self, team_id: Any, agent_id: Any) -> dict[str, Any]:
        """An agent leaves: its open errands go to the chief, or are cancelled (11.3)."""
        team = self.get(team_id)
        if not isinstance(agent_id, str) or self._repo.post(team["id"], agent_id) is None:
            raise AgencyError("That agent is not in this team.")
        self._repo.remove_post(team["id"], agent_id)
        now = self._clock.now()
        chief = team.get("chief_agent_id")
        if chief == agent_id:
            remaining = [post["agent_id"] for post in self._repo.posts(team["id"])]
            chief = remaining[0] if remaining else None
            team = self._repo.save(team["id"], {**team, "chief_agent_id": chief, "updated_at": now})
        for errand in self._repo.errands(team["id"], statuses=OPEN_STATUSES):
            if errand.get("to_agent_id") != agent_id:
                continue
            if chief is not None:
                self._repo.save_errand(
                    errand["id"],
                    {
                        **errand,
                        "to_agent_id": chief,
                        "status": "pending",
                        "session_id": None,
                        "turn_id": None,
                        "updated_at": now,
                    },
                )
            else:
                self._repo.save_errand(
                    errand["id"],
                    {
                        **errand,
                        "status": "cancelled",
                        "return_reason": "its agent left",
                        "updated_at": now,
                    },
                )
        self._changed(team["id"], "updated")
        return self.view(team)

    def teams_of(self, agent_id: str) -> list[dict[str, Any]]:
        """The live teams an agent works in (it has a post there)."""
        return [
            team
            for post in self._repo.posts_of_agent(agent_id)
            if (team := self._repo.get(post["team_id"])) is not None
            and team.get("archived_at") is None
        ]

    def team_of(self, agent_id: str, ref: Any) -> dict[str, Any] | None:
        """One of the agent's teams, by id or by name (any case)."""
        wanted = str(ref or "").strip().lstrip("@")
        for team in self.teams_of(agent_id):
            if team["id"] == wanted or team["name"].lower() == wanted.lower():
                return team
        return None

    def delivery_target(self, agent_id: str, raw: Any) -> dict[str, Any]:
        """Where an agent's rule delivers its result, with ids (vision 10.1).

        {kind: office, team}: posted in the office of one of its teams, as
        the agent. {kind: agent, agent, team?}: a teammate is mentioned in the
        office they share. Teams and agents go by id or by name.
        """
        if not isinstance(raw, dict) or raw.get("kind") not in DELIVERY_KINDS:
            raise AgencyError(
                "deliver_to must be {kind: office, team} or {kind: agent, agent, team?}."
            )
        team_ref = raw.get("team_id") or raw.get("team")
        if raw["kind"] == "office":
            team = self.team_of(agent_id, team_ref)
            if team is None:
                raise AgencyError("The office must be one of a team this agent works in.")
            return {"kind": "office", "team_id": team["id"]}
        if team_ref:
            team = self.team_of(agent_id, team_ref)
            if team is None:
                raise AgencyError("The team must be one this agent works in.")
            teams = [team]
        else:
            teams = self.teams_of(agent_id)
        ref = str(raw.get("agent_id") or raw.get("agent") or "").strip().lstrip("@")
        for team in teams:
            for post in self._repo.posts(team["id"]):
                mate = self._agents.get(post["agent_id"])
                if mate["id"] != ref and mate["name"].lower() != ref.lower():
                    continue
                if mate["id"] == agent_id:
                    raise AgencyError("A rule delivers to a teammate, not to its own agent.")
                return {"kind": "agent", "agent_id": mate["id"], "team_id": team["id"]}
        raise AgencyError("The agent must be a teammate (in a team with this rule's agent).")

    # -- errands ---------------------------------------------------------------------

    def create_errand(
        self,
        team_id: Any,
        raw: dict[str, Any],
        *,
        from_agent_id: str | None = None,
        from_team_id: str | None = None,
        chain_id: str | None = None,
        hop: int = 0,
        external: bool = False,
    ) -> dict[str, Any]:
        """A new errand on a team's board (D-3). To nobody: the chief takes it (D-8).

        `external`: it was written by a turn that read something from outside
        the machine, so the turn that does it keeps the guard on.
        """
        team = self.get(team_id)
        if team.get("archived_at") is not None:
            raise AgencyError("An archived team takes no errands.")
        if not isinstance(raw, dict):
            raise AgencyError("An errand needs its title.")
        title = _text(raw.get("title"), "The title", MAX_ERRAND_TITLE, required=True)
        body = _text(raw.get("body"), "The body", MAX_ERRAND_BODY)
        done_when = normalize_done_when(raw.get("done_when"))
        to_agent = raw.get("to_agent_id") or None
        members = [post["agent_id"] for post in self._repo.posts(team["id"])]
        if to_agent is not None and to_agent not in members:
            raise AgencyError("That agent is not in this team.")
        if to_agent is None:
            to_agent = team.get("chief_agent_id")
        now = self._clock.now()
        errand_id = self._ids.new("errand")
        saved = self._repo.save_errand(
            errand_id,
            {
                "team_id": team["id"],
                "from_team_id": from_team_id,
                "from_agent_id": from_agent_id,
                "to_agent_id": to_agent,
                "title": title,
                "body": body,
                "done_when": done_when,
                "status": "pending",
                "chain_id": chain_id or self._ids.new("chain"),
                "hop": hop,
                "production_run_id": raw.get("production_run_id"),
                "external": external,
                "created_at": now,
                "updated_at": now,
            },
        )
        if to_agent is None and self.on_stopped is not None:
            # No chief yet: it waits on the board, and the owner is told.
            self.on_stopped(team["id"], saved["chain_id"], "no_chief")
        self._changed(team["id"], "errand")
        return saved

    def errand(self, errand_id: Any) -> dict[str, Any]:
        if not isinstance(errand_id, str) or not errand_id:
            raise AgencyError("An errand id is needed.")
        errand = self._repo.errand(errand_id)
        if errand is None:
            raise LookupError(f"Unknown errand: {errand_id}")
        return errand

    def review(
        self, errand_id: Any, accepted: bool, reason: str | None = None, *, external: bool = False
    ) -> dict:
        """Whoever asked reviews a delivered errand: accepted, or back with why.

        `external`: the reviewing turn read something from outside the machine.
        Its reason goes to whoever redoes the errand, so the errand keeps the
        mark and the turn that redoes it keeps the guard on (#75).
        """
        errand = self.errand(errand_id)
        if errand["status"] != "delivered":
            raise AgencyError("Only a delivered errand can be reviewed.")
        now = self._clock.now()
        if accepted:
            saved = self._repo.save_errand(
                errand["id"], {**errand, "status": "reviewed", "updated_at": now}
            )
        else:
            why = _text(reason, "The reason", MAX_DONE_ITEM, required=True)
            returns = int(errand.get("returns") or 0) + 1
            too_many = returns > MAX_RETURNS
            saved = self._repo.save_errand(
                errand["id"],
                {
                    **errand,
                    # Sent back too many times: it stops until the owner retries it.
                    "status": "blocked" if too_many else "returned",
                    "return_reason": "too_many_returns" if too_many else why,
                    "returns": returns,
                    "external": bool(errand.get("external")) or external,
                    "session_id": None,
                    "turn_id": None,
                    "updated_at": now,
                },
            )
            if too_many and self.on_stopped is not None:
                self.on_stopped(errand["team_id"], errand["chain_id"], "too_many_returns")
        self._changed(errand["team_id"], "errand")
        return saved

    def retry(self, errand_id: Any) -> dict[str, Any]:
        """The owner puts a blocked errand back on the board."""
        errand = self.errand(errand_id)
        if errand["status"] != "blocked":
            raise AgencyError("Only a blocked errand can be retried.")
        saved = self._repo.save_errand(
            errand["id"],
            {**errand, "status": "pending", "returns": 0, "updated_at": self._clock.now()},
        )
        self._changed(errand["team_id"], "errand")
        return saved

    # -- templates (10.4) ------------------------------------------------------------

    def template(self, team_id: Any) -> dict[str, Any]:
        """A team as a template: its jobs, instructions, marked connectors and budget.

        Never its accounts nor its credentials: connectors are copied by name
        only, and the team's own accounts (owner team:ID) stay behind.
        """
        team = self.get(team_id)
        posts = self._repo.posts(team["id"])
        agents = []
        chief_index = None
        for index, post in enumerate(posts):
            agent = self._agents.get(post["agent_id"])
            if post["agent_id"] == team.get("chief_agent_id"):
                chief_index = index
            agents.append({field: agent.get(field) for field in EDITABLE})
        return {
            "name": team["name"],
            "purpose": team.get("purpose") or "",
            "connectors": list(team.get("connectors") or []),
            "budget": dict(team.get("budget") or {}),
            "agents": agents,
            "chief_index": chief_index,
        }

    def duplicate(self, team_id: Any, name: Any, *, new_agents: bool = True) -> dict[str, Any]:
        """A new team from another one («Canal infantil» → «Canal de recetas»).

        With new_agents, each member is copied as a new agent of its own (its
        memory and accounts do not travel). Without, the same agents take a
        post in the new team too.
        """
        source = self.get(team_id)
        template = self.template(source["id"])
        profile = source["rinari_profile_id"]
        members: list[str] = []
        for post, card in zip(self._repo.posts(source["id"]), template["agents"], strict=True):
            if not new_agents:
                members.append(post["agent_id"])
                continue
            # Same jobs, a fresh agent: its memory and accounts do not travel.
            copied = self._agents.create(
                {key: value for key, value in card.items() if value is not None}, profile
            )
            members.append(copied["id"])
        chief = members[template["chief_index"]] if template["chief_index"] is not None else None
        return self.create(
            {
                "name": name,
                "purpose": template["purpose"],
                "connectors": template["connectors"],
                "budget": template["budget"],
                "members": members,
                "chief_agent_id": chief,
            },
            profile,
        )

    def cancel(self, errand_id: Any) -> dict[str, Any]:
        errand = self.errand(errand_id)
        if errand["status"] not in OPEN_STATUSES:
            return errand
        saved = self._repo.save_errand(
            errand["id"], {**errand, "status": "cancelled", "updated_at": self._clock.now()}
        )
        self._changed(errand["team_id"], "errand")
        return saved

    def _changed(self, team_id: str, change: str) -> None:
        if self.on_changed is not None:
            self.on_changed(team_id, change)
