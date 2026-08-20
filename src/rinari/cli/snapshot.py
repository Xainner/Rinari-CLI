"""Runtime snapshot: the single source of truth for the CLI UI (phase 7).

The banner, status rail, slash commands and JSON status all render FROM this
object; none of them re-derives metrics on their own (harness observability
rule: display only what real data supports — unknown metrics render as
`None`/`—`, never as guessed numbers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rinari.models.types import Usage

if TYPE_CHECKING:  # pragma: no cover
    from rinari.cli.agent_runtime import AgentSession


@dataclass
class SessionUsage:
    """Cumulative usage for the current CLI run of a session.

    Token dimensions stay `None` until the provider reports them at least
    once; `cost_usd` stays `None` unless pricing data is available.
    """

    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    elapsed_s: float = 0.0
    cost_usd: float | None = None

    @property
    def tokens_total(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    def merge_turn(
        self,
        usage: Usage | None,
        *,
        tool_calls: int,
        model_calls: int,
        elapsed_delta_s: float,
        input_price_per_mtok: float | None,
        output_price_per_mtok: float | None,
    ) -> None:
        self.model_calls += model_calls
        self.tool_calls += tool_calls
        self.elapsed_s += elapsed_delta_s
        if usage is not None:
            self.input_tokens = (self.input_tokens or 0) + (usage.input_tokens or 0)
            self.output_tokens = (self.output_tokens or 0) + (usage.output_tokens or 0)
            if usage.cached_input_tokens is not None:
                self.cached_input_tokens = (
                    self.cached_input_tokens or 0
                ) + usage.cached_input_tokens
            if usage.reasoning_tokens is not None:
                self.reasoning_tokens = (self.reasoning_tokens or 0) + usage.reasoning_tokens
            # Cost only with real pricing; one unknown price half = unmeasured.
            if input_price_per_mtok is not None and output_price_per_mtok is not None:
                base = self.cost_usd or 0.0
                base += ((usage.input_tokens or 0) / 1_000_000) * input_price_per_mtok + (
                    (usage.output_tokens or 0) / 1_000_000
                ) * output_price_per_mtok
                self.cost_usd = base


@dataclass(frozen=True)
class RuntimeSnapshot:
    version: str
    # session
    session_id: str
    session_kind: str
    session_state: str
    mode: str
    # provider / model
    provider_alias: str | None
    provider_type: str | None
    model_alias: str | None
    provider_model_id: str | None
    reasoning_effort: str | None
    # context
    context_used_tokens: int | None
    context_window_tokens: int | None
    context_percent: float | None
    # project
    project_name: str | None
    branch: str | None
    dirty: bool | None
    # policy / network
    profile: str
    network_mode: str | None
    # capabilities
    tools_loaded: int
    skills_active: tuple[str, ...]
    skills_known: int
    agents: tuple[dict[str, Any], ...]
    # validation
    last_completion: dict[str, Any] | None
    # usage (this CLI run)
    usage: SessionUsage = field(default_factory=SessionUsage, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "session": {
                "id": self.session_id,
                "kind": self.session_kind,
                "state": self.session_state,
                "mode": self.mode,
            },
            "provider": {
                "alias": self.provider_alias,
                "type": self.provider_type,
            },
            "model": {
                "alias": self.model_alias,
                "provider_model_id": self.provider_model_id,
                "reasoning_effort": self.reasoning_effort,
            },
            "context": {
                "used_tokens": self.context_used_tokens,
                "window_tokens": self.context_window_tokens,
                "percent": self.context_percent,
            },
            "project": {
                "name": self.project_name,
                "branch": self.branch,
                "dirty": self.dirty,
            },
            "policy": {"profile": self.profile},
            "network": {"mode": self.network_mode},
            "capabilities": {
                "tools_loaded": self.tools_loaded,
                "skills_active": list(self.skills_active),
                "skills_known": self.skills_known,
                "agents": list(self.agents),
            },
            "validation": {"last_completion": self.last_completion},
            "usage": {
                "model_calls": self.usage.model_calls,
                "tool_calls": self.usage.tool_calls,
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "cached_input_tokens": self.usage.cached_input_tokens,
                "reasoning_tokens": self.usage.reasoning_tokens,
                "elapsed_s": round(self.usage.elapsed_s, 1),
                "cost_usd": self.usage.cost_usd,
            },
        }


def _model_record(session: AgentSession):
    if not session.record.model_id:
        return None
    try:
        return session.services.ctx.model_repo.get(session.record.model_id)
    except Exception:
        return None


def _pricing(model):
    settings = getattr(model, "settings", None) or {}
    try:
        pin = float(settings["input_price_per_mtok"])
        pout = float(settings["output_price_per_mtok"])
        return pin, pout
    except (KeyError, TypeError, ValueError):
        return None, None


def estimate_context_used(context) -> int | None:
    """Rough token estimate of the in-memory history (chars/4 heuristic).

    Used only for the UI progress cue when no provider usage number is
    available for the current position; real numbers come from `Usage`.
    """
    try:
        messages = list(context.history)
    except Exception:
        return None
    if not messages:
        return 0
    chars = 0
    for message in messages:
        content = getattr(message, "content", None)
        if content:
            chars += len(content)
        for call in getattr(message, "tool_calls", ()) or ():
            chars += len(str(getattr(call, "arguments", {})))
    return max(1, chars // 4)


def build_snapshot(session: AgentSession) -> RuntimeSnapshot:
    from rinari import __version__

    record = session.record
    try:
        provider = session.services.providers.get(record.provider_id)
    except Exception:
        provider = None
    model = _model_record(session)

    context = session.context
    window: int | None = None
    try:
        window = session.loop._provider.capabilities().max_context_tokens
    except Exception:
        window = None
    used = estimate_context_used(context)
    percent = min(1.0, used / window) if (used is not None and window) else None

    project_name = None
    branch = record.git_branch
    dirty = None
    root = Path(record.project_root_snapshot) if record.project_root_snapshot else None
    if root is not None and root.exists():
        project_name = root.name
        from rinari.projects.git import git_state

        try:
            state = git_state(root)
            if state.available:
                branch = state.branch or branch
                dirty = state.dirty
        except Exception:
            pass

    profile = record.profile_id or "workspace"
    try:
        network_mode = session.services.network.policy().mode
    except Exception:
        network_mode = None

    tools_loaded = 0
    try:
        tools_loaded = len(session.loop._tools.registry.names())
    except Exception:
        tools_loaded = 0

    skills_active = tuple(name for name, _ in (record.active_skills or ()))
    skills_known = 0
    try:
        skills_known = len(session.services.skills.summaries(root, session=record))
    except Exception:
        skills_known = 0

    agents: tuple[dict[str, Any], ...] = ()
    if session.orchestrator is not None:
        try:
            agents = tuple(session.orchestrator.list())
        except Exception:
            agents = ()

    usage = session.usage if isinstance(session.usage, SessionUsage) else SessionUsage()

    return RuntimeSnapshot(
        version=__version__,
        session_id=record.id,
        session_kind=record.kind,
        session_state=record.state,
        mode=record.mode,
        provider_alias=provider.alias if provider is not None else None,
        provider_type=provider.type if provider is not None else None,
        model_alias=model.alias if model is not None else None,
        provider_model_id=model.provider_model_id if model is not None else None,
        reasoning_effort=_reasoning_effort(session),
        context_used_tokens=used,
        context_window_tokens=window,
        context_percent=percent,
        project_name=project_name,
        branch=branch,
        dirty=dirty,
        profile=profile,
        network_mode=network_mode,
        tools_loaded=tools_loaded,
        skills_active=skills_active,
        skills_known=skills_known,
        agents=agents,
        last_completion=getattr(session, "last_completion", None),
        usage=usage,
    )


def _reasoning_effort(session: AgentSession) -> str | None:
    model = _model_record(session)
    if model is None:
        return None
    settings = getattr(model, "settings", None) or {}
    effort = settings.get("reasoning_effort")
    return str(effort) if effort else None


def merge_usage_for_turn(
    session: AgentSession,
    turn_usage: Usage | None,
    *,
    tool_calls: int,
    model_calls: int,
    elapsed_s: float,
) -> None:
    model = _model_record(session)
    pin, pout = _pricing(model) if model is not None else (None, None)
    session.usage.merge_turn(
        turn_usage,
        tool_calls=tool_calls,
        model_calls=model_calls,
        elapsed_delta_s=max(0.0, elapsed_s),
        input_price_per_mtok=pin,
        output_price_per_mtok=pout,
    )


__all__ = [
    "RuntimeSnapshot",
    "SessionUsage",
    "build_snapshot",
    "estimate_context_used",
    "merge_usage_for_turn",
]
