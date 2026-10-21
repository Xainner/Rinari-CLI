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
ERRAND_MODES = ("turn", "subagents")
#: The ways an errand can run that exist today (D-28: subagents come later).
AVAILABLE_ERRAND_MODES = ("turn",)


def agency_settings(services: Any) -> dict[str, Any]:
    """The Agency's own settings: the global daily budget and how errands run."""
    stored = services.ctx.config_repo.get_json(SETTINGS_KEY) or {}
    budget = stored.get("budget") if isinstance(stored, dict) else None
    mode = stored.get("errand_mode") if isinstance(stored, dict) else None
    stopped = stored.get("stopped_profiles") if isinstance(stored, dict) else None
    return {
        "budget": budget if isinstance(budget, dict) else {},
        "errand_mode": mode if mode in AVAILABLE_ERRAND_MODES else "turn",
        "errand_modes": list(ERRAND_MODES),
        "available_errand_modes": list(AVAILABLE_ERRAND_MODES),
        # Profiles whose board «Detener a todos» halted, until the owner resumes it.
        "stopped_profiles": list(stopped) if isinstance(stopped, list) else [],
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
        profile = self._server._profile_filter(params)
        agents = self.agency.list(profile, include_archived=include)
        return {"agents": [self._view(agent) for agent in agents]}

    def get(self, params: dict[str, Any]) -> dict[str, Any]:
        agent = self._agent(params)
        sessions = self._services.sessions.list(agent_id=agent["id"], limit=50)
        return {
            "agent": self._view(agent),
            "sessions": [session_to_dict(record) for record in sessions],
        }

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
        agent = self._agent(params)
        return {"agent": self._view(self.agency.archive(agent["id"]))}

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
        agents = {agent["id"] for agent in self.agency.list(profile)}
        sessions = {
            session_id
            for agent_id in agents
            for session_id in self._services.ctx.agency_repo.session_ids(agent_id)
        }
        cancelled = []
        for turn in self._turns.runtime_state()["active_turns"]:
            if turn["session_id"] in sessions and turn["session_id"] not in cancelled:
                try:
                    self._turns.cancel_turn(turn["session_id"])
                    cancelled.append(turn["session_id"])
                except Exception:  # already finishing: nothing left to stop
                    continue
        paused = []
        for task in self._services.ctx.schedule_repo.list():
            if task.get("agent_id") in agents and task.get("enabled"):
                # A direct write: pausing never fails on a rule that no longer
                # validates (a watched folder that was deleted, for example).
                self._services.ctx.schedule_repo.set_enabled(
                    task["id"], False, self._services.ctx.clock.now()
                )
                paused.append(task["id"])
        # The board too: nothing queued starts until the owner resumes (B5).
        board = getattr(self._server, "_teams", None)
        halted = (
            board.halt(profile or self._services.rinari_profiles.active_id())
            if board is not None
            else {"blocked_errands": 0}
        )
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
        """What the Agency card says: how many agents, working and waiting."""
        profile = self._server._profile_filter(params)
        agents = self.agency.list(profile)
        sessions = {
            session_id: agent["id"]
            for agent in agents
            for session_id in self._services.ctx.agency_repo.session_ids(agent["id"])
        }
        state = self._turns.runtime_state()
        working = {
            sessions[turn["session_id"]]
            for turn in state["active_turns"]
            if turn["session_id"] in sessions
        }
        waiting = {
            sessions[item["session_id"]]
            for item in state["pending_approvals"]
            if item.get("session_id") in sessions
        }
        return {
            "agents": len(agents),
            "working": len(working),
            "needs_you": len(waiting),
            "agent_ids": {"working": sorted(working), "needs_you": sorted(waiting)},
        }
