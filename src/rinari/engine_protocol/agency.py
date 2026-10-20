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
