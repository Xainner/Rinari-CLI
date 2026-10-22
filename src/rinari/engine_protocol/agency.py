"""Rinari Agency in the Engine: the agents' methods, their spend and status.

An agent's direct chat is an ordinary CHAT conversation marked with its id,
so it runs like any other turn; what changes is read from the agent's card
when the turn is built (model, permission mode, instructions, connectors).

Spend is what the usage report already knows (tokens priced with the
catalog) over the agent's conversations, today. A model without a known
price has no known cost: the cost is then reported as unknown, never
invented, and only a token cap can stop that agent.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from rinari.agency import AgencyError
from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.engine_protocol.messages import event
from rinari.engine_protocol.snapshots import session_to_dict
from rinari.shared.errors import NotFoundError

if TYPE_CHECKING:
    from rinari.engine_protocol.server import EngineServer


def checked_owner(services: Any, owner: Any) -> str | None:
    """`owner` of a new connector account: None, "agent:<id>" of an existing
    agent or, A2, "team:<id>" of an existing team (only its posts see it)."""
    if owner is None:
        return None
    kind, _, ref = owner.partition(":") if isinstance(owner, str) else ("", "", "")
    exists = False
    if kind == "agent" and ref:
        exists = services.ctx.agency_repo.get(ref) is not None
    elif kind == "team" and ref:
        exists = services.ctx.team_repo.get(ref) is not None
    if not exists:
        raise EngineProtocolError(
            INVALID_PARAMS,
            "Param 'owner' must be 'agent:<id>' of an existing agent or 'team:<id>' of an "
            "existing team.",
        )
    return owner


SETTINGS_KEY = "agency.settings"
#: The profiles a «Detener a todos» with no profile halted: what «Reanudar» with none resumes.
HALTED_BY_ALL_KEY = "agency.halted_by_all"
ERRAND_MODES = ("turn", "subagents")
#: The ways an errand can run that exist today (D-28: subagents come later).
AVAILABLE_ERRAND_MODES = ("turn",)


def halt_profiles(services: Any, agents: list[dict[str, Any]]) -> set[str]:
    """The profiles «Detener a todos» halts when it names none: every one with agents
    or teams, and the active one."""
    profiles = {agent["rinari_profile_id"] for agent in agents}
    profiles |= {team["rinari_profile_id"] for team in services.ctx.team_repo.list(None)}
    profiles.add(services.rinari_profiles.active_id())
    return profiles


def agency_settings(services: Any) -> dict[str, Any]:
    """The Agency's own settings: the global daily budget and how errands run."""
    stored = services.ctx.config_repo.get_json(SETTINGS_KEY) or {}
    budget = stored.get("budget") if isinstance(stored, dict) else None
    mode = stored.get("errand_mode") if isinstance(stored, dict) else None
    stopped = stored.get("stopped_profiles") if isinstance(stored, dict) else None
    teams = stored.get("stopped_teams") if isinstance(stored, dict) else None
    return {
        "budget": budget if isinstance(budget, dict) else {},
        "errand_mode": mode if mode in AVAILABLE_ERRAND_MODES else "turn",
        "errand_modes": list(ERRAND_MODES),
        "available_errand_modes": list(AVAILABLE_ERRAND_MODES),
        # Profiles whose board «Detener a todos» halted, until the owner resumes it.
        "stopped_profiles": list(stopped) if isinstance(stopped, list) else [],
        # Teams the owner stopped alone («Detener equipo»), until he resumes them.
        "stopped_teams": list(teams) if isinstance(teams, list) else [],
    }


def spend_of(services: Any, scope: str, ref: str | None, budget: dict[str, Any]) -> dict:
    """Today's spend of an agent, a team or the whole Agency, against its cap."""
    from rinari.engine_protocol.usage_report import build_report

    report = build_report(services, scope, ref)
    totals = report["totals"]
    tokens = int(totals.get("input_tokens") or 0) + int(totals.get("output_tokens") or 0)
    priced = all(row.get("cost") is not None for row in report["models"])
    cost = totals.get("cost") if priced else None
    over = False
    if budget.get("daily_tokens") is not None and tokens >= budget["daily_tokens"]:
        over = True
    if budget.get("daily_usd") is not None and cost is not None and cost >= budget["daily_usd"]:
        over = True
    return {
        "tokens": tokens,
        "cost_usd": cost,
        "cost_known": priced,
        "calls": int(totals.get("calls") or 0),
        "budget": budget,
        "over_budget": over,
    }


def spend_today(services: Any, agent: dict[str, Any]) -> dict[str, Any]:
    """Today's spend of an agent and whether it is over its daily cap."""
    from rinari.engine_protocol.usage_report import build_report

    report = build_report(services, "agent", agent["id"])
    totals = report["totals"]
    tokens = int(totals.get("input_tokens") or 0) + int(totals.get("output_tokens") or 0)
    priced = all(row.get("cost") is not None for row in report["models"])
    cost = totals.get("cost") if priced else None
    budget = agent.get("budget") or {}
    over = False
    if budget.get("daily_tokens") is not None and tokens >= budget["daily_tokens"]:
        over = True
    if budget.get("daily_usd") is not None and cost is not None and cost >= budget["daily_usd"]:
        over = True
    return {
        "tokens": tokens,
        "cost_usd": cost,
        # False when a model it used has no known price: the cost is unknown.
        "cost_known": priced,
        "calls": int(totals.get("calls") or 0),
        "budget": budget,
        "over_budget": over,
    }


def forget_place(services: Any, place: str, ref: str) -> int:
    """Forget every memory kept for an agent or a team being deleted."""
    gone = 0
    for row in services.memory.repo.user_live():
        mine = row.get("place") == place and row.get("place_ref") == ref
        if mine and services.memory.forget_user(row["id"]):
            gone += 1
    return gone


class AgencyMethods:
    def __init__(self, server: EngineServer) -> None:
        self._server = server
        self._services = server._services
        self._turns = server._turns
        agency = self._services.agency
        agency.open_chat = self._open_chat
        agency.on_changed = self._changed

    @property
    def agency(self):
        return self._services.agency

    # -- wiring ------------------------------------------------------------------

    def _open_chat(self, agent: dict[str, Any]) -> str:
        """The agent's direct chat: a CHAT conversation in the agent's profile."""
        created = self._server._session_create(
            {
                "chat": True,
                "title": agent["name"],
                "permission_profile": agent["permission_profile"],
                "rinari_profile_id": agent["rinari_profile_id"],
            }
        )
        session_id = created["session"]["id"]
        if agent.get("model"):
            self._services.sessions.set_model(session_id, agent["model"], None)
        return session_id

    def _changed(self, agent_id: str, change: str) -> None:
        self._turns.emit_external(
            event("agency.agent.changed", {"agent_id": agent_id, "change": change})
        )

    def _agent(self, params: dict[str, Any]) -> dict[str, Any]:
        agent_id = params.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            raise EngineProtocolError(
                INVALID_PARAMS, "Param 'agent_id' must be a non-empty string."
            )
        try:
            return self.agency.get(agent_id)
        except LookupError:
            raise NotFoundError(f"Unknown agent: {agent_id}") from None

    def _view(self, agent: dict[str, Any]) -> dict[str, Any]:
        return {**agent, "spend_today": spend_today(self._services, agent)}

    # -- methods -----------------------------------------------------------------

    def list(self, params: dict[str, Any]) -> dict[str, Any]:
        include = params.get("include_archived", False)
        if not isinstance(include, bool):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'include_archived' must be a boolean.")
        archived = params.get("archived", False)
        if not isinstance(archived, bool):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'archived' must be a boolean.")
        profile = self._server._profile_filter(params)
        # archived: only the archived ones (to bring one back); include_archived: all.
        agents = self.agency.list(profile, include_archived=include, archived_only=archived)
        return {"agents": [self._view(agent) for agent in agents]}

    def get(self, params: dict[str, Any]) -> dict[str, Any]:
        """The card, and its conversations with what each one is (E12): its direct
        chat, a post (with its team), a run of one of its rules (with the task and
        the run) or a child of one of those."""
        agent = self._agent(params)
        limit = params.get("limit", 100)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'limit' must be an int in 1..500.")
        sessions = self._services.sessions.list(agent_id=agent["id"], limit=limit)
        return {
            "agent": self._view(agent),
            "sessions": [
                {**session_to_dict(record), "agency": self._session_kind(agent, record)}
                for record in sessions
            ],
        }

    def _session_kind(self, agent: dict[str, Any], record: Any) -> dict[str, Any]:
        repo = self._services.ctx.team_repo
        if record.id == agent.get("session_id"):
            return {"kind": "direct"}
        post = repo.post_for_session(record.id)
        if post is not None:
            team = repo.get(post["team_id"])
            return {
                "kind": "post",
                "team_id": post["team_id"],
                "team_name": team["name"] if team else None,
            }
        if getattr(record, "parent_session_id", None):
            return {"kind": "child", "parent_session_id": record.parent_session_id}
        if getattr(record, "team_id", None):
            # The post of a team it left: still readable, no longer working there.
            team = repo.get(record.team_id)
            return {
                "kind": "former_post",
                "team_id": record.team_id,
                "team_name": team["name"] if team else None,
            }
        if getattr(record, "lineage_kind", None) == "schedule":
            brief = getattr(record, "brief", None) or {}
            return {
                "kind": "rule_run",
                "task_id": brief.get("task_id"),
                "run_id": brief.get("run_id"),
            }
        return {"kind": "other"}

    def create(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = params.get("agent")
        if not isinstance(raw, dict):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'agent' must be an object.")
        profile = self._server._profile_param(params) or (
            self._services.rinari_profiles.active_id()
        )
        try:
            agent = self.agency.create(raw, profile)
        except AgencyError as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from None
        return {"agent": self._view(agent)}

    def update(self, params: dict[str, Any]) -> dict[str, Any]:
        agent = self._agent(params)
        patch = params.get("patch")
        if not isinstance(patch, dict):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'patch' must be an object.")
        try:
            updated = self.agency.update(agent["id"], patch)
        except AgencyError as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from None
        # The direct chat follows the card's model and permission mode.
        if updated.get("session_id"):
            if "permission_profile" in patch:
                self._server._session_permission_set(
                    {
                        "ref": updated["session_id"],
                        "permission_profile": updated["permission_profile"],
                    }
                )
            if patch.get("model"):
                self._services.sessions.set_model(updated["session_id"], updated["model"], None)
        return {"agent": self._view(updated)}

    def archive(self, params: dict[str, Any]) -> dict[str, Any]:
        """Archive an agent (E5): it stops, leaves every team and its rules are paused.

        Leaving is `agency.team.leave`: its post's turn is cut, its open errands
        go to the chief and, if it was the chief, the next member is (or no one,
        and the office says so). Its rules are switched off and stay off if it
        comes back. Its conversations and spend stay readable.
        """
        from rinari.engine_protocol.agency_teams import PAUSED_BY_ARCHIVE

        agent = self._agent(params)
        if agent.get("archived_at") is not None:
            return {"agent": self._view(agent), "left_teams": [], "paused_rules": 0}
        board = getattr(self._server, "_teams", None)
        left = []
        for team in self._services.teams.teams_of(agent["id"]):
            if board is not None:
                board.leave({"team_id": team["id"], "agent_id": agent["id"]})
            else:
                self._services.teams.leave(team["id"], agent["id"])
            left.append(team["id"])
        repo = self._services.ctx.schedule_repo
        now = self._services.ctx.clock.now()
        paused = []
        for task in repo.list():
            if task.get("agent_id") != agent["id"]:
                continue
            if task.get("enabled") or task.get("paused_by"):
                # Off by the archive, whatever paused it before (a stop's resume
                # must not switch on a rule of an archived agent).
                repo.set_enabled(task["id"], False, now, paused_by=PAUSED_BY_ARCHIVE)
                paused.append(task["id"])
        for session_id in self._services.ctx.agency_repo.session_ids(agent["id"]):
            if self._turns.has_active_turn(session_id):
                try:
                    self._turns.cancel_turn(session_id)
                except Exception:  # already over
                    continue
        archived = self.agency.archive(agent["id"])
        for task_id in paused:
            self._turns.emit_external(
                event("schedule.changed", {"task_id": task_id, "change": "updated"})
            )
        return {"agent": self._view(archived), "left_teams": left, "paused_rules": len(paused)}

    def unarchive(self, params: dict[str, Any]) -> dict[str, Any]:
        """Bring an archived agent back (E5): not into its teams, its rules still off."""
        agent = self._agent(params)
        return {"agent": self._view(self.agency.unarchive(agent["id"]))}

    def delete(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delete an archived agent for good: its conversations, rules and memory
        (owner, 22-oct). Archive first: that is what takes it out of its teams and
        stops its rules and turns."""
        agent = self._agent(params)
        if agent.get("archived_at") is None:
            raise EngineProtocolError(INVALID_PARAMS, "Archive the agent before deleting it.")
        repo = self._services.ctx.agency_repo
        sessions = sorted(set(repo.session_ids(agent["id"])) | ({agent.get("session_id")} - {None}))
        running = [sid for sid in sessions if self._turns.has_active_turn(sid)]
        if running:
            raise EngineProtocolError(
                INVALID_PARAMS,
                "A conversation of this agent is still finishing a turn: try again in a moment.",
                details={"session_ids": running},
            )
        rules = [
            task["id"]
            for task in self._services.ctx.schedule_repo.list()
            if task.get("agent_id") == agent["id"]
        ]
        for task_id in rules:
            self._services.ctx.schedule_repo.delete(task_id)
            self._turns.emit_external(
                event("schedule.changed", {"task_id": task_id, "change": "deleted"})
            )
        deleted = 0
        for session_id in sessions:
            try:
                self._server._session_delete({"ref": session_id, "cascade": True})
                deleted += 1
            except NotFoundError:
                continue
        forget_place(self._services, "agent", agent["id"])
        repo.delete(agent["id"])
        self._turns.emit_external(
            event("agency.agent.changed", {"agent_id": agent["id"], "change": "deleted"})
        )
        return {"deleted": agent["id"], "deleted_sessions": deleted, "deleted_rules": len(rules)}

    def _draft_caller(self):
        """The owner's default model (the one a new conversation uses), or None."""
        from rinari.models.router import ModelRouter
        from rinari.runtime.model_caller import ModelCaller

        selection = self._services.providers.current()
        if selection is None or selection.model is None:
            return None
        router = ModelRouter(self._services.providers, self._services.models)
        return ModelCaller(router, selection.provider, selection.model.id)

    def draft(self, params: dict[str, Any]) -> dict[str, Any]:
        """«Créalo con Rinari»: a draft of a card from one sentence. Creates nothing.

        The Engine serves one request at a time and a model call takes
        seconds: this answers at once with a job id, and the draft arrives as
        `agency.draft.ready` (or `agency.draft.failed`, with a plain reason).
        """
        from rinari.agency.draft import (
            DRAFT_MAX_TOKENS,
            MAX_DESCRIPTION,
            SYSTEM_PROMPT,
            parse_draft,
        )
        from rinari.models.types import ChatMessage, ModelRequest

        description = params.get("description")
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > MAX_DESCRIPTION
        ):
            raise EngineProtocolError(
                INVALID_PARAMS, f"Param 'description' must be 1..{MAX_DESCRIPTION} characters."
            )
        caller = self._draft_caller()
        if caller is None:
            raise EngineProtocolError(
                INVALID_PARAMS, "Connect a provider with a model first: the draft uses it."
            )
        job_id = self._services.ctx.ids.new("draft")

        def run() -> None:
            try:
                response = caller.invoke(
                    ModelRequest(
                        model=caller.model_id or "",
                        messages=(
                            ChatMessage.system(SYSTEM_PROMPT),
                            ChatMessage.user(description.strip()),
                        ),
                        max_tokens=DRAFT_MAX_TOKENS,
                    )
                )
                draft = parse_draft(response.content or "")
            except Exception as exc:  # the desktop must hear back
                message = getattr(exc, "message", None) or str(exc)
                self._turns.emit_external(
                    event("agency.draft.failed", {"job_id": job_id, "message": message[:500]})
                )
                return
            if draft is None:
                self._turns.emit_external(
                    event(
                        "agency.draft.failed",
                        {
                            "job_id": job_id,
                            "message": "The model did not return a usable draft. "
                            "Try again or fill in the form.",
                        },
                    )
                )
                return
            self._turns.emit_external(
                event("agency.draft.ready", {"job_id": job_id, "draft": draft})
            )

        threading.Thread(target=run, name="agency-draft", daemon=True).start()
        return {"status": "started", "job_id": job_id}

    def memory_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """An agent's own memory (D-24, Ajustes de la Agency). Edit and forget
        use memory.update and memory.forget, like any personal memory."""
        return self._place_memory("agent", self._agent(params)["id"])

    def memory_add(self, params: dict[str, Any]) -> dict[str, Any]:
        """The owner writes something into an agent's own memory."""
        return self._remember(params, "agent", self._agent(params)["id"])

    def _team(self, params: dict[str, Any]) -> dict[str, Any]:
        team_id = params.get("team_id")
        if not isinstance(team_id, str) or not team_id:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'team_id' must be a non-empty string.")
        team = self._services.ctx.team_repo.get(team_id)
        if team is None:
            raise NotFoundError(f"Unknown team: {team_id}")
        return team

    def team_memory_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """A2: a team's memory, what its posts learn (D-7)."""
        return self._place_memory("team", self._team(params)["id"])

    def team_memory_add(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._remember(params, "team", self._team(params)["id"])

    def _place_memory(self, place: str, ref: str) -> dict[str, Any]:
        rows = [
            row
            for row in self._services.memory.list_user(limit=500)
            if row.get("place") == place and row.get("place_ref") == ref
        ]
        return {"records": self._server._with_place_labels(rows), "count": len(rows)}

    def _remember(self, params: dict[str, Any], place: str, ref: str) -> dict[str, Any]:
        from rinari.shared.errors import InvalidUsageError

        topic = params.get("topic")
        text = params.get("text")
        kind = params.get("kind", "fact")
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 128:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'topic' is required, max 128.")
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'text' is required, max 4096.")
        if kind not in ("preference", "rule", "fact"):
            raise EngineProtocolError(
                INVALID_PARAMS, "Param 'kind' must be preference, rule, or fact."
            )
        try:
            result = self._services.memory.remember_user(
                text.strip(),
                kind=kind,
                topic=topic.strip(),
                provenance="panel",
                place=place,
                place_ref=ref,
            )
        except InvalidUsageError as exc:
            raise EngineProtocolError(INVALID_PARAMS, exc.message) from None
        row = self._services.memory.get_user(result["id"])
        return {"record": self._server._with_place_labels([row])[0] if row else None}

    def settings_get(self, params: dict[str, Any]) -> dict[str, Any]:
        settings = agency_settings(self._services)
        return {
            **settings,
            "spend_today": spend_of(self._services, "agency", None, settings["budget"]),
        }

    def settings_set(self, params: dict[str, Any]) -> dict[str, Any]:
        """The global daily budget and how errands run (D-28)."""
        from rinari.agency.service import normalize_budget
        from rinari.shared.clock import now_iso

        current = agency_settings(self._services)
        stored = {
            "budget": current["budget"],
            "errand_mode": current["errand_mode"],
            "stopped_profiles": current["stopped_profiles"],
            "stopped_teams": current["stopped_teams"],
        }
        if "budget" in params:
            try:
                stored["budget"] = normalize_budget(params.get("budget"))
            except AgencyError as exc:
                raise EngineProtocolError(INVALID_PARAMS, str(exc)) from None
        if "errand_mode" in params:
            mode = params.get("errand_mode")
            if mode not in AVAILABLE_ERRAND_MODES:
                raise EngineProtocolError(
                    INVALID_PARAMS,
                    "That way of running errands does not exist yet."
                    if mode in ERRAND_MODES
                    else "Unknown errand_mode.",
                )
            stored["errand_mode"] = mode
        self._services.ctx.config_repo.set_json(
            SETTINGS_KEY, stored, now_iso(self._services.ctx.clock)
        )
        return self.settings_get({})

    def stop_all(self, params: dict[str, Any]) -> dict[str, Any]:
        """«Detener a todos» (section 8): stop every agent of the profile now.

        Cancels the turns running in their conversations and pauses their
        active rules, so nothing starts again on its own. Nothing is lost:
        the owner turns the rules back on in «Reglas». Rinari's own
        conversations are not touched.
        """
        profile = self._server._profile_filter(params)
        listed = self.agency.list(profile)
        agents = {agent["id"] for agent in listed}
        sessions = {
            session_id
            for agent_id in agents
            for session_id in self._services.ctx.agency_repo.session_ids(agent_id)
        }
        # The board first (B5): halted before any turn is cut, so the tick a
        # cancelled turn runs finds nothing to start. It cuts its own live turns.
        # Every profile whose agents are stopped (all of them with no profile, #75).
        board = getattr(self._server, "_teams", None)
        halted = {"blocked_errands": 0}
        if board is not None:
            profiles = {profile} if profile else halt_profiles(self._services, listed)
            for each in sorted(profiles):
                halted["blocked_errands"] += board.halt(each, by_all=not profile)["blocked_errands"]
        from rinari.engine_protocol.agency_teams import PAUSED_BY_STOP

        paused = []
        for task in self._services.ctx.schedule_repo.list():
            if task.get("agent_id") in agents and task.get("enabled"):
                # A direct write: pausing never fails on a rule that no longer
                # validates (a watched folder that was deleted, for example).
                # Marked, so «Reanudar» switches back on only these (#18).
                self._services.ctx.schedule_repo.set_enabled(
                    task["id"], False, self._services.ctx.clock.now(), paused_by=PAUSED_BY_STOP
                )
                paused.append(task["id"])
        cancelled = []
        for turn in self._turns.runtime_state()["active_turns"]:
            if turn["session_id"] in sessions and turn["session_id"] not in cancelled:
                try:
                    self._turns.cancel_turn(turn["session_id"])
                    cancelled.append(turn["session_id"])
                except Exception:  # already finishing: nothing left to stop
                    continue
        self._turns.emit_external(
            event(
                "agency.stopped",
                {
                    "cancelled_sessions": cancelled,
                    "paused_rules": paused,
                    "blocked_errands": halted["blocked_errands"],
                },
            )
        )
        for task_id in paused:
            self._turns.emit_external(
                event("schedule.changed", {"task_id": task_id, "change": "updated"})
            )
        return {
            "cancelled_turns": len(cancelled),
            "paused_rules": len(paused),
            "blocked_errands": halted["blocked_errands"],
        }

    def status(self, params: dict[str, Any]) -> dict[str, Any]:
        """What the Agency card says: how many agents, working and waiting (E9).

        `needs_you` counts the agents waiting for an approval plus what only
        the owner can move: delivered errands he reviews and stopped chains he
        has not dismissed (`waiting_for_owner`). `session_ids` says which
        conversations work or wait, for the app to mark each post. `stopped`:
        «Detener a todos» halted this profile (the active one with no profile).
        """
        profile = self._server._profile_filter(params)
        agents = self.agency.list(profile)
        sessions = {
            session_id: agent["id"]
            for agent in agents
            for session_id in self._services.ctx.agency_repo.session_ids(agent["id"])
        }
        state = self._turns.runtime_state()
        working_sessions = {
            turn["session_id"] for turn in state["active_turns"] if turn["session_id"] in sessions
        }
        waiting_sessions = {
            item["session_id"]
            for item in state["pending_approvals"]
            if item.get("session_id") in sessions
        }
        working = {sessions[session_id] for session_id in working_sessions}
        waiting = {sessions[session_id] for session_id in waiting_sessions}
        board = getattr(self._server, "_teams", None)
        owner = (
            board.waiting_for_owner(profile) if board is not None else {"errands": 0, "chains": 0}
        )
        stopped_profiles = set(agency_settings(self._services)["stopped_profiles"])
        return {
            "agents": len(agents),
            "working": len(working),
            "needs_you": len(waiting) + owner["errands"] + owner["chains"],
            "agent_ids": {"working": sorted(working), "needs_you": sorted(waiting)},
            "session_ids": {
                "working": sorted(working_sessions),
                "needs_you": sorted(waiting_sessions),
            },
            "stopped": (profile or self._services.rinari_profiles.active_id()) in stopped_profiles,
            "waiting_for_owner": owner,
        }

    # -- templates (E11, «Usar una plantilla») -----------------------------------

    def templates_list(self, params: dict[str, Any]) -> dict[str, Any]:
        from rinari.agency.templates import TemplateError, templates

        try:
            return {"templates": templates(params.get("language"))}
        except TemplateError as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from None

    def team_from_template(self, params: dict[str, Any]) -> dict[str, Any]:
        """A team and its agents from a built-in template, in the profile (the active one).

        `model`: the model every new agent uses (else the owner's default). No
        connector, account nor budget comes with a template.
        """
        from rinari.agency.templates import TemplateError, template

        try:
            chosen = template(params.get("template_id"), params.get("language"))
        except TemplateError as exc:
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from None
        except LookupError:
            raise NotFoundError(f"Unknown template: {params.get('template_id')}") from None
        model = params.get("model")
        if model is not None and not isinstance(model, str):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'model' must be a string.")
        name = params.get("name") or chosen["team"]["name"]
        profile = self._server._profile_param(params) or self._services.rinari_profiles.active_id()
        board = getattr(self._server, "_teams", None)
        created: list[dict[str, Any]] = []
        try:
            for card in chosen["agents"]:
                created.append(
                    self.agency.create({**card, **({"model": model} if model else {})}, profile)
                )
            team = self._services.teams.create(
                {
                    "name": name,
                    "purpose": chosen["team"]["purpose"],
                    "members": [agent["id"] for agent in created],
                },
                profile,
            )
        except AgencyError as exc:
            # Nothing half made: the agents created so far are archived again.
            for agent in created:
                self.agency.archive(agent["id"])
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from None
        if board is not None:
            board.tick()
        return {"team": team, "agents": [self._view(agent) for agent in created]}
