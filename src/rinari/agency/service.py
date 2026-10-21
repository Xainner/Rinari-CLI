"""Rinari Agency agents: their card and what it allows.

An agent is created and changed only by the owner (the desktop form, or by
accepting a draft that Rinari filled in): the model never creates one. Its
card says who it is (name, role, instructions), how it works (model,
permission mode) and what it may use (connectors, daily budget). Its direct
chat is an ordinary conversation marked with its id.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

PERMISSION_PROFILES = ("read-only", "workspace", "full-access")
MAX_NAME = 60
MAX_ROLE = 120
MAX_INSTRUCTIONS = 8000
MAX_CONNECTORS = 50
MAX_DAILY_USD = 10_000.0
MAX_DAILY_TOKENS = 1_000_000_000
EDITABLE = (
    "name",
    "role",
    "instructions",
    "model",
    "permission_profile",
    "connectors",
    "budget",
    "ask_new_destinations",
)


class AgencyError(ValueError):
    """Invalid input for an agent; the message is safe to show."""


def _text(raw: Any, field: str, limit: int, *, required: bool = False) -> str:
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise AgencyError(f"{field} must be text.")
    value = raw.strip()
    if required and not value:
        raise AgencyError(f"{field} must be 1..{limit} characters.")
    if len(value) > limit:
        raise AgencyError(f"{field} must be at most {limit} characters.")
    return value


def normalize_budget(raw: Any) -> dict[str, Any]:
    """A daily cap in USD, in tokens, or none ({}).

    Tokens exist because a model without a known price has no known cost,
    and a cost is never invented: its agent can only be capped in tokens.
    """
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict):
        raise AgencyError("The budget must be {daily_usd} or {daily_tokens}.")
    unknown = set(raw) - {"daily_usd", "daily_tokens"}
    if unknown:
        raise AgencyError(f"Unknown budget fields: {', '.join(sorted(unknown))}.")
    budget: dict[str, Any] = {}
    usd = raw.get("daily_usd")
    if usd is not None:
        if isinstance(usd, bool) or not isinstance(usd, (int, float)):
            raise AgencyError("daily_usd must be a number.")
        if not 0 < float(usd) <= MAX_DAILY_USD:
            raise AgencyError(f"daily_usd must be more than 0 and at most {MAX_DAILY_USD:g}.")
        budget["daily_usd"] = round(float(usd), 4)
    tokens = raw.get("daily_tokens")
    if tokens is not None:
        if isinstance(tokens, bool) or not isinstance(tokens, int):
            raise AgencyError("daily_tokens must be a whole number.")
        if not 0 < tokens <= MAX_DAILY_TOKENS:
            raise AgencyError(f"daily_tokens must be 1..{MAX_DAILY_TOKENS}.")
        budget["daily_tokens"] = tokens
    return budget


class AgencyService:
    def __init__(
        self,
        repo: Any,
        clock: Any,
        ids: Any,
        *,
        resolve_model: Callable[[str], str | None] = lambda model: model,
        connector_exists: Callable[[str], bool] = lambda name: True,
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._ids = ids
        # The model's id (the card keeps the id, whatever the owner typed).
        self._resolve_model = resolve_model
        self._connector_exists = connector_exists
        # Set by the Engine server: opens the agent's direct chat and returns
        # its session id, and announces a change (agency.agent.changed).
        self.open_chat: Callable[[dict[str, Any]], str] | None = None
        self.on_changed: Callable[[str, str], None] | None = None

    # -- validation --------------------------------------------------------

    def _normalize(self, raw: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
        unknown = set(raw) - set(EDITABLE)
        if unknown:
            raise AgencyError(f"Unknown fields: {', '.join(sorted(unknown))}.")
        merged = {**(current or {}), **raw}
        agent: dict[str, Any] = {
            "name": _text(merged.get("name"), "The name", MAX_NAME, required=True),
            "role": _text(merged.get("role"), "The role", MAX_ROLE),
            "instructions": _text(merged.get("instructions"), "The instructions", MAX_INSTRUCTIONS),
        }
        model = merged.get("model") or None
        if model is not None:
            resolved = self._resolve_model(model) if isinstance(model, str) else None
            if resolved is None:
                raise AgencyError(f"Unknown model: {model}.")
            model = resolved
        agent["model"] = model
        profile = merged.get("permission_profile") or "workspace"
        if profile not in PERMISSION_PROFILES:
            raise AgencyError(
                f"permission_profile must be one of {', '.join(PERMISSION_PROFILES)}."
            )
        agent["permission_profile"] = profile
        connectors = merged.get("connectors") or []
        if not isinstance(connectors, list) or not all(isinstance(c, str) for c in connectors):
            raise AgencyError("connectors must be a list of connector names.")
        if len(connectors) > MAX_CONNECTORS:
            raise AgencyError(f"An agent can have at most {MAX_CONNECTORS} connectors.")
        seen: list[str] = []
        for name in connectors:
            if name in seen:
                continue
            if not self._connector_exists(name):
                raise AgencyError(f"Unknown connector: {name}.")
            seen.append(name)
        agent["connectors"] = seen
        agent["budget"] = normalize_budget(merged.get("budget"))
        ask = merged.get("ask_new_destinations")
        if ask is not None and not isinstance(ask, bool):
            raise AgencyError("ask_new_destinations must be true, false or null.")
        agent["ask_new_destinations"] = ask
        return agent

    # -- reads -------------------------------------------------------------

    def get(self, agent_id: Any) -> dict[str, Any]:
        if not isinstance(agent_id, str) or not agent_id:
            raise AgencyError("An agent id is needed.")
        agent = self._repo.get(agent_id)
        if agent is None:
            raise LookupError(f"Unknown agent: {agent_id}")
        return agent

    def list(self, rinari_profile_id: str | None, *, include_archived: bool = False) -> list[dict]:
        return self._repo.list(rinari_profile_id, include_archived=include_archived)

    def for_session(self, session_id: str | None) -> dict[str, Any] | None:
        """The agent whose conversation this is, or None (also when archived)."""
        if not session_id:
            return None
        agent = self._repo.for_session(session_id)
        if agent is None or agent.get("archived_at") is not None:
            return None
        return agent

    # -- writes ------------------------------------------------------------

    def create(self, raw: dict[str, Any], rinari_profile_id: str = "default") -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AgencyError("An agent card is needed.")
        agent = self._normalize(raw, None)
        now = self._clock.now()
        agent_id = self._ids.new("agent")
        saved = self._repo.save(
            agent_id,
            {**agent, "rinari_profile_id": rinari_profile_id, "created_at": now, "updated_at": now},
        )
        if self.open_chat is not None:
            session_id = self.open_chat(saved)
            self._repo.set_session_agent(session_id, agent_id)
            saved = self._repo.save(agent_id, {**saved, "session_id": session_id})
        self._changed(agent_id, "created")
        return saved

    def update(self, agent_id: Any, raw: dict[str, Any]) -> dict[str, Any]:
        current = self.get(agent_id)
        if current.get("archived_at") is not None:
            raise AgencyError("An archived agent cannot be changed.")
        if not isinstance(raw, dict):
            raise AgencyError("The changes must be an object.")
        agent = self._normalize(raw, current)
        saved = self._repo.save(
            current["id"], {**current, **agent, "updated_at": self._clock.now()}
        )
        self._changed(current["id"], "updated")
        return saved

    def archive(self, agent_id: Any) -> dict[str, Any]:
        """Archived, never deleted: its conversations and spend stay readable."""
        current = self.get(agent_id)
        if current.get("archived_at") is not None:
            return current
        now = self._clock.now()
        saved = self._repo.save(current["id"], {**current, "archived_at": now, "updated_at": now})
        self._changed(current["id"], "archived")
        return saved

    def _changed(self, agent_id: str, change: str) -> None:
        if self.on_changed is not None:
            self.on_changed(agent_id, change)
