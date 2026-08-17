"""Storage-aware compaction service (phase 4).

`maybe_compact` is invoked by the agent loop (through an injected hook)
before model calls. When context pressure crosses the compact threshold it:

1. rebuilds a provider-independent `CompactState` from the conversation
   (rule-based extraction) plus persisted evidence (tasks, validation
   records, approvals from PolicyDecision events, artifacts);
2. trims the in-memory history tail in place (safe cut) so the next request
   fits the post-compact budget;
3. persists the state into `sessions.compact_state_json` (survives resume)
   and records a `ContextCompacted` event.

The persisted conversation (`session_messages`) is never truncated: resume
restores full history and re-applies the same tail selection deterministically.
"""

from __future__ import annotations

import contextlib
import dataclasses

from rinari.artifacts.store import ArtifactStore
from rinari.context import compact_state, engine, tokens
from rinari.models.types import ChatMessage
from rinari.prompts.assembler import PromptAssembler
from rinari.runtime.agent import AgentContext
from rinari.storage.records import SessionEventRecord
from rinari.verify.records import VALIDATION_KINDS

MAX_APPROVALS_RECORDED = 20
MAX_VALIDATION_KINDS = 8


class ContextService:
    def __init__(self, ctx, artifacts: ArtifactStore) -> None:
        self._ctx = ctx
        self._artifacts = artifacts

    # -- evidence -----------------------------------------------------------

    def build_evidence(self, session_id: str, project_root: str | None) -> dict:
        evidence: dict = {}
        if project_root:
            tasks = self._ctx.task_repo.list(project_root)
            evidence["tasks_completed"] = [
                t["title"] or t["id"] for t in tasks if t.get("status") == "done"
            ]
            evidence["tasks_active"] = [
                t["title"] or t["id"]
                for t in tasks
                if t.get("status") in ("in_progress", "pending")
            ]
            evidence["tasks_blocked"] = [
                t["title"] or t["id"] for t in tasks if t.get("status") == "blocked"
            ]
            validations: list[str] = []
            blockers: list[str] = []
            for kind in VALIDATION_KINDS[:MAX_VALIDATION_KINDS]:
                latest = self._ctx.validation_repo.latest(project_root, kind)
                if latest is None:
                    continue
                line = f"{kind}: {latest['result']}"
                if latest.get("command"):
                    line += f" ({latest['command']})"
                validations.append(line)
                if latest["result"] in ("failed", "error"):
                    blockers.append(f"validation {kind} is {latest['result']}")
            evidence["validations"] = validations
            evidence["blockers"] = blockers
            evidence["project_root"] = project_root

        approvals: list[str] = []
        for event in self._ctx.event_repo.list(session_id, limit=500):
            if event.type != "PolicyDecision":
                continue
            payload = event.payload or {}
            if payload.get("action") != "ask":
                continue
            target = payload.get("target")
            line = f"{payload.get('capability')}"
            if target:
                line += f" {target}"
            approvals.append(line)
        evidence["approvals"] = approvals[-MAX_APPROVALS_RECORDED:]

        evidence["artifacts"] = [
            record.uri() for record in self._artifacts.list(session_id=session_id)
        ]
        return evidence

    # -- compaction ---------------------------------------------------------

    def maybe_compact(
        self,
        agent_ctx: AgentContext,
        *,
        session_id: str,
        window_tokens: int | None,
        used_input_tokens: int | None = None,
        history: list[ChatMessage] | None = None,
    ) -> bool:
        """Compact if pressure >= PRESSURE_COMPACT. Returns True when it did."""
        window = tokens.resolve_context_window(window_tokens)
        history = agent_ctx.history if history is None else history
        system_length = len(
            PromptAssembler().build(dataclasses.replace(agent_ctx.assembler_base)).system_prompt
        )
        estimated = tokens.estimate_tokens(system_prompt=" " * system_length, history=history)
        used = max(estimated, used_input_tokens or 0)
        pressure_value = tokens.pressure(used, window)
        if pressure_value is None or pressure_value < tokens.PRESSURE_COMPACT:
            return False

        record = self._ctx.session_repo.get(session_id)
        project_root = record.project_root_snapshot if record is not None else None
        state = compact_state.extract_from_history(tuple(history))
        evidence = self.build_evidence(session_id, project_root)
        if record is not None and record.model_id:
            provider = self._ctx.provider_repo.get(record.provider_id)
            provider_alias = provider.alias if provider is not None else ""
            evidence["provider_model"] = (
                f"{provider_alias}/{record.model_id}" if provider_alias else record.model_id
            )
        evidence["compacted_at"] = self._now()
        state = compact_state.merge_evidence(state, evidence)

        tail_budget = int(window * tokens.POST_COMPACT_KEEP_RATIO)
        kept = engine.select_history(history, budget_tokens=tail_budget)
        dropped = len(history) - len(kept)
        if dropped <= 0:
            return False

        # In-place: the loop holds this same list for the next request.
        agent_ctx.history.clear()
        agent_ctx.history.extend(kept)
        agent_ctx.dropped_total += dropped
        agent_ctx.compact_state_text = state.render_prompt()
        agent_ctx.compacted = True

        self._persist_state(record, state)
        self._persist_event(
            session_id,
            "ContextCompacted",
            {
                "pressure": round(pressure_value, 3),
                "window_tokens": window,
                "dropped_messages": dropped,
                "kept_messages": len(kept),
            },
        )
        return True

    def restore_compact_state(self, agent_ctx: AgentContext) -> None:
        """Resume/reconcile: reapply a persisted compact state, if any."""
        record = self._ctx.session_repo.get(agent_ctx.session_id)
        if record is None or not record.compact_state:
            return
        state = compact_state.CompactState.from_dict(record.compact_state)
        if not state.is_empty():
            agent_ctx.compact_state_text = state.render_prompt()

    # -- internals -----------------------------------------------------------

    def _now(self) -> str:
        from rinari.shared.clock import now_iso

        return now_iso(self._ctx.clock)

    def _persist_state(self, record, state: compact_state.CompactState) -> None:
        if record is None:
            return
        updated = dataclasses.replace(
            record,
            compact_state=state.to_dict(),
            updated_at=self._now(),
        )
        with contextlib.suppress(Exception):
            self._ctx.session_repo.update(updated)

    def _persist_event(self, session_id: str, event_type: str, payload: dict) -> None:
        with contextlib.suppress(Exception):
            self._ctx.event_repo.insert(
                SessionEventRecord(
                    id=self._ctx.ids.new("evt"),
                    session_id=session_id,
                    seq=self._ctx.event_repo.next_seq(session_id),
                    type=event_type,
                    payload=payload,
                    created_at=self._now(),
                )
            )


__all__ = ["ContextService"]
