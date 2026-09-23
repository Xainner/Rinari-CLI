"""Storage-aware context services.

Production session hosts call ``prepare`` before model dispatch. It generates a
cumulative summary and persists exact covered message IDs before replacing the
active projection. Original messages remain intact. ``maybe_compact`` retains
the legacy deterministic hook for compatibility; production uses ``prepare``.
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

    def prepare(self, *args):
        from rinari.context.preparation import prepare

        return prepare(self, *args)

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
            if event.type != "ToolApproved":
                continue
            payload = event.payload or {}
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
        from rinari.models.types import ModelRequest
        from rinari.models.visual_context import select_visual_context

        kept = list(
            select_visual_context(
                ModelRequest(model="", messages=tuple(kept)), compact=True
            ).messages
        )
        dropped = len(history) - len(kept)
        retired_media = sum(len(m.images) for m in history) - sum(len(m.images) for m in kept)
        if dropped <= 0 and retired_media <= 0:
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
                "retired_images": retired_media,
            },
        )
        return True

    def restore_compact_state(self, agent_ctx: AgentContext) -> None:
        """Resume/reconcile: reapply a persisted compact state, if any."""
        record = self._ctx.session_repo.get(agent_ctx.session_id)
        if record is None:
            return
        state = record.compact_state or {}
        agent_ctx.compact_revision = int(state.get("revision") or 0)
        self._restore_usage_anchor(agent_ctx)
        if not state:
            return
        from rinari.context.projection import render

        agent_ctx.compact_state_text = render(state)

    def _restore_usage_anchor(self, agent_ctx: AgentContext) -> None:
        """Reuse the last provider-reported usage if it measured this context.

        It calibrates the estimate only for the same model and the same
        projection; after a compaction or a model change it is dropped.
        """
        from rinari.runtime.agent import EVENT_MODEL_INVOKED

        event = self._ctx.event_repo.latest(agent_ctx.session_id, [EVENT_MODEL_INVOKED])
        anchor = (event.payload or {}).get("context_anchor") if event else None
        if (
            isinstance(anchor, dict)
            and anchor.get("model") == agent_ctx.model_ref
            and anchor.get("compact_revision") == agent_ctx.compact_revision
            and type(anchor.get("actual")) is int
            and type(anchor.get("estimated")) is int
        ):
            agent_ctx.context_usage = {
                "model": anchor["model"],
                "estimated": anchor["estimated"],
                "actual": anchor["actual"],
            }

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
