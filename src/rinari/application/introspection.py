"""Read-only views of Rinari's own state, for the model (`rinari.*` tools).

Model tools cannot read the engine home, so questions such as "what happened in
session X" had no direct path: the model had to improvise shell and SQL over
several calls. These views answer in one call each, with bounded, redacted,
presentation-safe data built from what the engine already persists.

Turns are rebuilt from both event layers: the desktop activity events
(`turn.*`, `model.*`, `tool.*`, `usage.updated`) carry a turn id; the harness
events (`AgentTurnStarted`, `ModelInvoked`, `ToolCompleted`, ...), which the
terminal also writes, are grouped by order.
"""

from __future__ import annotations

import importlib.metadata
import json
import platform
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rinari.shared.redaction import redact_text

PREVIEW = 240
ANSWER_PREVIEW = 600
MAX_SESSIONS = 50
MAX_TURNS = 20
MAX_EVENTS = 200
MAX_TOOLS = 25

TURN_START = ("turn.started", "AgentTurnStarted")
TURN_END = (
    "turn.completed",
    "turn.failed",
    "turn.cancelled",
    "turn.stopped",
    "TurnInterrupted",
    "AgentTurnCompleted",
)
MODEL_EVENTS = (
    "model.started",
    "model.completed",
    "model.failed",
    "model.content.completed",
    "ModelInvoked",
)
TOOL_EVENTS = (
    "tool.requested",
    "tool.completed",
    "tool.failed",
    "tool.cancelled",
    "ToolRequested",
    "ToolCompleted",
    "ToolFailed",
)
OTHER_EVENTS = (
    "usage.updated",
    "governor.compact",
    "governor.stop",
    "approval.requested",
    "approval.resolved",
    "question.requested",
    "LoopDetected",
    "turn.changes.completed",
    "verification.completed",
    "SkillActivated",
)
TURN_EVENTS = (*TURN_START, *TURN_END, *MODEL_EVENTS, *TOOL_EVENTS, *OTHER_EVENTS)


def _clip(value: Any, limit: int = PREVIEW) -> str | None:
    if value is None:
        return None
    text = redact_text(str(value)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    """Drop empty fields: every key costs the model context."""
    return {k: v for k, v in row.items() if v not in (None, [], {}, "")}


def _arguments(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            value = str(value)
    return _clip(value, 160)


def _error_text(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("message") or value.get("code") or value
    return _clip(value, 300)


@dataclass
class _Turn:
    id: str
    started_at: str | None = None
    message: str | None = None
    mode: str | None = None
    reasoning_effort: str | None = None
    agent_index: int | None = None
    models: list[str] = field(default_factory=list)
    model_calls: int = 0
    finish_reasons: list[str] = field(default_factory=list)
    model_errors: list[str] = field(default_factory=list)
    answer: str | None = None
    tools: list[dict] = field(default_factory=list)
    requested: dict[str, dict] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    ended: str | None = None
    ended_at: str | None = None
    end_kind: str | None = None
    end_error: str | None = None
    wall_time_s: float | None = None
    compactions: list[dict] = field(default_factory=list)
    approvals_requested: int = 0
    approvals_resolved: int = 0
    loop_detected: bool = False
    changes: dict | None = None
    events: list[Any] = field(default_factory=list)


class Introspection:
    """Views over one Rinari home; every returned string is redacted."""

    def __init__(self, services) -> None:
        self._services = services
        self._ctx = services.ctx

    # -- status ------------------------------------------------------------

    def status(
        self, project=None, include_models: bool = False, provider: str | None = None
    ) -> dict[str, Any]:
        """Engine, providers and settings; models only when asked (they add up)."""
        from rinari.context.settings import load as context_settings
        from rinari.engine_protocol.protocol import PROTOCOL_VERSION
        from rinari.providers.metadata import model_metadata
        from rinari.soul.store import SoulStore

        ctx = self._ctx
        providers = {p.id: p for p in self._services.providers.list()}
        models = self._services.models.list()
        settings = context_settings(ctx)
        manual = settings.get("model_windows") or {}
        aliases = {m.id: m.alias for m in models}
        per_provider: dict[str, int] = {}
        for model in models:
            per_provider[model.provider_id] = per_provider.get(model.provider_id, 0) + 1
        wanted = (provider or "").lower()

        def matches(model) -> bool:
            owner = providers.get(model.provider_id)
            names = {model.provider_id.lower(), (owner.alias if owner else "").lower()}
            return wanted in names if wanted else include_models

        listed = [m for m in models if matches(m)]
        model_rows = []
        for model in listed[:100]:
            owner = providers.get(model.provider_id)
            discovered = (model.settings or {}).get("discovered_capabilities") or {}
            override = model.capabilities or {}
            catalog = model_metadata(owner, model.provider_model_id) if owner else {}
            window, source = None, "unknown"
            for label, value in (
                ("manual", manual.get(model.id)),
                ("override", override.get("max_context_tokens")),
                ("provider", discovered.get("max_context_tokens")),
                ("catalog", (catalog or {}).get("max_context_tokens")),
            ):
                if _int(value):
                    window, source = value, label
                    break
            model_rows.append(
                _compact(
                    {
                        "id": model.id,
                        "alias": model.alias,
                        "provider": owner.alias if owner else None,
                        "provider_model_id": model.provider_model_id,
                        "availability": model.availability,
                        "window_tokens": window,
                        "window_source": source,
                        "vision": discovered.get("vision", override.get("vision")),
                        "reasoning": bool(
                            discovered.get("reasoning_effort") or override.get("reasoning")
                        )
                        or None,
                    }
                )
            )
        provider_rows = []
        for provider in providers.values():
            try:
                has_credential = bool(self._services.providers.credential_ref(provider))
            except Exception:
                has_credential = None
            provider_rows.append(
                _compact(
                    {
                        "id": provider.id,
                        "alias": provider.alias,
                        "type": provider.type,
                        "auth_method": provider.auth_method,
                        "endpoint": _clip(provider.endpoint, 200),
                        "connected": provider.status_connected,
                        "checked_at": provider.status_checked_at,
                        "has_credential": has_credential,
                        "models": per_provider.get(provider.id, 0),
                        "default_model": aliases.get(provider.default_model_id or ""),
                    }
                )
            )
        try:
            skills = [row["name"] for row in self._services.skills.summaries(project)]
        except Exception:
            skills = []
        try:
            version = importlib.metadata.version("rinari")
        except importlib.metadata.PackageNotFoundError:
            version = None
        return {
            "engine": {
                "version": version,
                "protocol_version": PROTOCOL_VERSION,
                "home": str(ctx.layout.root),
                "platform": platform.system(),
            },
            "providers": provider_rows,
            **({"models": model_rows} if listed else {}),
            "models_total": len(models),
            "context": {
                "auto_compaction": settings.get("enabled"),
                "compact_at_percent": settings.get("compact_at_percent"),
                "summarizer": aliases.get(settings.get("model_id") or "", "conversation model"),
                "manual_windows": len(manual),
            },
            "soul": SoulStore(ctx.layout.root).active_id(),
            "skills": skills,
            "sessions_total": len(ctx.session_repo.list()),
        }

    # -- sessions ----------------------------------------------------------

    def sessions(
        self,
        query: str | None = None,
        limit: int = 20,
        kind: str | None = None,
        since: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(MAX_SESSIONS, int(limit or 20)))
        records = self._ctx.session_repo.list(kind=kind.upper() if kind else None)
        aliases = {m.id: m.alias for m in self._services.models.list()}
        if since:
            records = [r for r in records if (r.last_active_at or "") >= since]
        if model:
            wanted = model.lower()
            records = [
                r
                for r in records
                if wanted in (aliases.get(r.model_id or "", "") or "").lower()
                or wanted == (r.model_id or "").lower()
            ]
        matched_by: dict[str, str] = {}
        if query:
            needle = query.strip().lower()
            text_hits = self._sessions_with_text(needle)
            kept = []
            for record in records:
                if needle in (record.title or "").lower() or needle == record.id.lower():
                    matched_by[record.id] = "title"
                    kept.append(record)
                elif record.id in text_hits:
                    matched_by[record.id] = "message"
                    kept.append(record)
            records = kept
        total = len(records)
        records = records[:limit]
        counts = self._turn_counts([r.id for r in records])
        rows = []
        for record in records:
            last = self._ctx.event_repo.latest(record.id, TURN_END)
            rows.append(
                {
                    "id": record.id,
                    "title": _clip(record.title, 120),
                    "kind": record.kind,
                    "state": record.state,
                    "mode": record.mode,
                    "model": aliases.get(record.model_id or "", record.model_id),
                    "last_active_at": record.last_active_at,
                    "turns": counts.get(record.id, 0),
                    "last_turn": _end_status(last.type, last.payload) if last else None,
                    **({"matched": matched_by[record.id]} if record.id in matched_by else {}),
                }
            )
        return {"sessions": rows, "total": total, "truncated": total > len(rows)}

    def _sessions_with_text(self, needle: str) -> set[str]:
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._ctx.db.query(
            "SELECT DISTINCT session_id FROM session_messages "
            "WHERE lower(content) LIKE ? ESCAPE '\\' LIMIT 500",
            [f"%{escaped}%"],
        )
        return {row["session_id"] for row in rows}

    def _turn_counts(self, session_ids: list[str]) -> dict[str, int]:
        if not session_ids:
            return {}
        marks = ", ".join("?" for _ in session_ids)
        rows = self._ctx.db.query(
            f"SELECT session_id, type, COUNT(*) AS n FROM session_events "
            f"WHERE session_id IN ({marks}) AND type IN ('turn.started', 'AgentTurnStarted') "
            f"GROUP BY session_id, type",
            session_ids,
        )
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["session_id"]] = max(counts.get(row["session_id"], 0), row["n"])
        return counts

    # -- one session -------------------------------------------------------

    def session(self, session_id: str, turns_limit: int = 10) -> dict[str, Any]:
        record = self._record(session_id)
        turns_limit = max(1, min(MAX_TURNS, int(turns_limit or 10)))
        turns = self._turns(record.id)
        aliases = {m.id: m.alias for m in self._services.models.list()}
        providers = {p.id: p.alias for p in self._services.providers.list()}
        state = record.compact_state or {}
        shown = turns[-turns_limit:]
        return {
            "session": {
                "id": record.id,
                "title": _clip(record.title, 160),
                "kind": record.kind,
                "state": record.state,
                "mode": record.mode,
                "model": aliases.get(record.model_id or "", record.model_id),
                "provider": providers.get(record.provider_id or "", record.provider_id),
                "permission_profile": getattr(record, "permission_profile", None),
                "workspace": _clip(record.current_cwd, 300),
                "project_root": _clip(record.project_root_snapshot, 300),
                "created_at": record.created_at,
                "last_active_at": record.last_active_at,
                "forked_from": getattr(record, "forked_from", None),
                "active_skills": [name for name, _ in (record.active_skills or ())],
                "soul": getattr(record, "soul_id", None),
                "compaction_revision": state.get("revision") if isinstance(state, dict) else None,
                "messages": len(self._ctx.message_repo.list(record.id)),
            },
            "turns": [self._turn_view(t, aliases, last=t is turns[-1]) for t in shown],
            "turns_total": len(turns),
            "turns_shown": len(shown),
        }

    # -- one turn ----------------------------------------------------------

    def turn(
        self,
        turn_id: str,
        session_id: str | None = None,
        detail: str = "summary",
        limit: int = 100,
    ) -> dict[str, Any]:
        if not session_id:
            session_id = self._session_of_turn(turn_id)
            if session_id is None:
                raise LookupError(f"Unknown turn: {turn_id}. Pass session_id for terminal turns.")
        record = self._record(session_id)
        turns = self._turns(record.id)
        found = next((t for t in turns if t.id == turn_id), None)
        if found is None:
            raise LookupError(f"Turn {turn_id} is not in session {record.id}.")
        aliases = {m.id: m.alias for m in self._services.models.list()}
        view = self._turn_view(found, aliases, last=found is turns[-1])
        view["session_id"] = record.id
        view["tools"] = [
            _compact({k: v for k, v in tool.items() if k != "call"})
            for tool in found.tools[:MAX_TOOLS]
        ]
        if len(found.tools) > MAX_TOOLS:
            view["tools_truncated"] = len(found.tools) - MAX_TOOLS
        if found.changes:
            view["changes"] = found.changes
        if found.compactions:
            view["compactions"] = found.compactions
        if detail == "events":
            limit = max(1, min(MAX_EVENTS, int(limit or 100)))
            view["events"] = [_event_view(e) for e in found.events[:limit]]
            view["events_total"] = len(found.events)
        return view

    # -- turn reconstruction -------------------------------------------------

    def _record(self, session_id: str):
        record = self._ctx.session_repo.get(session_id)
        if record is None:
            raise LookupError(f"Unknown session: {session_id}")
        return record

    def _session_of_turn(self, turn_id: str) -> str | None:
        row = self._ctx.db.query_one(
            "SELECT session_id FROM session_events WHERE turn_id = ? LIMIT 1", [turn_id]
        )
        return row["session_id"] if row else None

    def _turns(self, session_id: str) -> list[_Turn]:
        events = self._ctx.event_repo.list(session_id, types=TURN_EVENTS)
        turns: list[_Turn] = []
        by_id: dict[str, _Turn] = {}
        current: _Turn | None = None
        for event in events:
            payload = event.payload if isinstance(event.payload, dict) else {}
            turn_id = event.turn_id or payload.get("turn_id")
            if event.type == "turn.started":
                current = _Turn(
                    id=str(turn_id or f"turn-seq-{event.seq}"),
                    started_at=payload.get("occurred_at") or event.created_at,
                    message=_clip(payload.get("message")),
                    mode=payload.get("mode"),
                    reasoning_effort=payload.get("reasoning_effort"),
                )
                turns.append(current)
                by_id[current.id] = current
                current.events.append(event)
                continue
            if event.type == "AgentTurnStarted":
                if current is None or current.agent_index is not None:
                    # A terminal turn: no desktop envelope opened it.
                    current = _Turn(
                        id=f"turn-index-{payload.get('turn_index', event.seq)}-{event.seq}",
                        started_at=event.created_at,
                        message=_clip(payload.get("preview")),
                    )
                    turns.append(current)
                current.agent_index = _int(payload.get("turn_index")) or 0
                current.events.append(event)
                continue
            target = by_id.get(str(turn_id)) if turn_id else current
            if target is None:
                continue
            target.events.append(event)
            _apply(target, event.type, payload, event.created_at)
        self._answers_from_messages(session_id, turns)
        return turns

    def _answers_from_messages(self, session_id: str, turns: list[_Turn]) -> None:
        """The answer of turns whose events carry no content (the terminal never emits it).

        Desktop messages name their turn; terminal ones are grouped from each
        user message and paired with the terminal turns from the latest back.
        """
        if all(turn.answer for turn in turns):
            return
        by_turn: dict[str, str] = {}
        groups: list[dict[str, str | None]] = []
        for message in self._ctx.message_repo.list(session_id):
            text = (message.content or "").strip()
            if message.turn_id:
                if message.role == "assistant" and text:
                    by_turn[message.turn_id] = message.content
            elif message.role == "user":
                groups.append({"answer": None})
            elif message.role == "assistant" and text and groups:
                groups[-1]["answer"] = message.content
        terminal = [t for t in turns if t.id.startswith("turn-index-")]
        for turn, group in zip(reversed(terminal), reversed(groups), strict=False):
            if not turn.answer and group["answer"]:
                turn.answer = group["answer"]
        for turn in turns:
            if not turn.answer and turn.id in by_turn:
                turn.answer = by_turn[turn.id]

    def _turn_view(self, turn: _Turn, aliases: dict[str, str], *, last: bool) -> dict[str, Any]:
        outcome = _outcome(turn, last=last)
        failed_tools = sum(1 for tool in turn.tools if tool.get("ok") is False)
        duration = turn.wall_time_s
        start, end = _time(turn.started_at), _time(turn.ended_at)
        if duration is None and start and end:
            duration = round((end - start).total_seconds(), 1)
        view: dict[str, Any] = {
            "id": turn.id,
            "started_at": turn.started_at,
            "message": turn.message,
            "outcome": outcome,
            "models": [aliases.get(m, m) for m in dict.fromkeys(turn.models)],
            "model_calls": turn.model_calls,
            "finish_reasons": list(dict.fromkeys(turn.finish_reasons)),
            "duration_s": duration,
            "input_tokens": turn.usage.get("input_tokens"),
            "output_tokens": turn.usage.get("output_tokens"),
            "tools_used": list(dict.fromkeys(tool["tool"] for tool in turn.tools)),
            "tool_calls": len(turn.tools),
            "tool_failures": failed_tools,
        }
        if turn.mode:
            view["mode"] = turn.mode
        if turn.answer:
            view["answer"] = _clip(turn.answer, ANSWER_PREVIEW)
        errors = [e for e in (turn.end_error, *turn.model_errors) if e]
        if errors:
            view["errors"] = list(dict.fromkeys(errors))[:5]
        anomalies = _anomalies(turn, outcome, failed_tools)
        if anomalies:
            view["anomalies"] = anomalies
        return _compact(view)


def _apply(turn: _Turn, kind: str, payload: dict, created_at: str) -> None:
    at = payload.get("occurred_at") or created_at
    if kind == "model.started":
        turn.model_calls += 1
        if payload.get("model"):
            turn.models.append(str(payload["model"]))
    elif kind == "ModelInvoked":
        if not any(e.type == "model.started" for e in turn.events):
            turn.model_calls += 1
        if payload.get("stop_reason"):
            turn.finish_reasons.append(str(payload["stop_reason"]))
        anchor = payload.get("context_anchor") or {}
        if not turn.models and isinstance(anchor, dict) and anchor.get("model"):
            turn.models.append(str(anchor["model"]))
        usage = payload.get("usage")
        if isinstance(usage, dict) and "output_tokens" not in turn.usage:
            turn.usage.setdefault("input_tokens", _int(usage.get("input_tokens")))
            turn.usage.setdefault("output_tokens", _int(usage.get("output_tokens")))
    elif kind == "model.completed":
        if payload.get("finish_reason"):
            turn.finish_reasons.append(str(payload["finish_reason"]))
    elif kind == "model.failed":
        error = _error_text(payload.get("error"))
        if error:
            turn.model_errors.append(error)
    elif kind == "model.content.completed":
        if payload.get("output_kind", "final") == "final" and payload.get("content"):
            turn.answer = str(payload["content"])
    elif kind in ("tool.requested", "ToolRequested"):
        call = str(payload.get("tool_call_id") or len(turn.requested))
        turn.requested[call] = {
            "tool": payload.get("tool") or payload.get("name"),
            "arguments": _arguments(payload.get("arguments")),
        }
    elif kind in ("tool.completed", "tool.failed", "tool.cancelled", "ToolCompleted", "ToolFailed"):
        call = str(payload.get("tool_call_id") or "")
        seen = {t.get("call") for t in turn.tools}
        if call and call in seen:
            return  # the desktop and harness layers report the same call
        request = turn.requested.get(call, {})
        ok = payload.get("ok")
        if kind in ("tool.failed", "ToolFailed"):
            ok = False
        elif kind == "tool.cancelled":
            ok = None
        turn.tools.append(
            {
                "call": call or None,
                "tool": payload.get("tool") or payload.get("name") or request.get("tool"),
                "ok": ok,
                "cancelled": kind == "tool.cancelled" or None,
                "error": _error_text(payload.get("error") or payload.get("error_code")),
                "duration_ms": _int(payload.get("duration_ms")),
                "arguments": request.get("arguments"),
            }
        )  # `call` only deduplicates the two event layers; views drop it
    elif kind == "usage.updated":
        for key in ("input_tokens", "output_tokens", "model_calls"):
            if _int(payload.get(key)) is not None:
                turn.usage[key] = payload[key]
    elif kind == "AgentTurnCompleted":
        budget = payload.get("budget") or {}
        if isinstance(budget, dict):
            for key in ("input_tokens", "output_tokens"):
                if key not in turn.usage and _int(budget.get(key)) is not None:
                    turn.usage[key] = budget[key]
            if isinstance(budget.get("wall_time_s"), (int, float)):
                turn.wall_time_s = float(budget["wall_time_s"])
        if turn.ended is None:
            turn.ended, turn.ended_at = "completed", at
    elif kind == "turn.completed":
        turn.ended, turn.ended_at, turn.end_kind = "completed", at, payload.get("kind")
        if payload.get("content") and not turn.answer:
            turn.answer = str(payload["content"])
    elif kind == "turn.failed":
        turn.ended, turn.ended_at = "failed", at
        turn.end_error = _error_text(payload.get("error") or payload.get("message"))
    elif kind in ("turn.cancelled", "turn.stopped", "TurnInterrupted"):
        turn.ended, turn.ended_at = "cancelled", at
    elif kind == "governor.compact":
        if payload.get("status") in ("completed", "failed", "skipped", "cancelled"):
            turn.compactions.append(
                {
                    k: payload.get(k)
                    for k in ("status", "reason", "used_tokens", "after_tokens", "duration_ms")
                    if payload.get(k) is not None
                }
            )
    elif kind == "approval.requested":
        turn.approvals_requested += 1
    elif kind == "approval.resolved":
        turn.approvals_resolved += 1
    elif kind == "LoopDetected":
        turn.loop_detected = True
    elif kind == "turn.changes.completed":
        files = payload.get("files") or []
        turn.changes = {
            "files": len(files) if isinstance(files, list) else None,
            "additions": payload.get("additions"),
            "deletions": payload.get("deletions"),
            "status": payload.get("status"),
        }


def _outcome(turn: _Turn, *, last: bool) -> str:
    if turn.ended == "failed" or (turn.model_errors and not turn.answer and not turn.tools):
        return "failed"
    if turn.ended == "cancelled":
        return "cancelled"
    if turn.ended is None:
        return "running" if last else "interrupted"
    if turn.answer:
        return "answered"
    if turn.tools:
        return "tools_only"
    return "empty"


def _anomalies(turn: _Turn, outcome: str, failed_tools: int) -> list[str]:
    found = []
    output = turn.usage.get("output_tokens") or 0
    if outcome in ("empty", "tools_only") and not turn.answer and output > 0 and not turn.tools:
        found.append(
            f"The provider reported {output} output tokens, but no text and no tool call "
            "were recorded: the response was not parsed (check how the provider streams "
            "its output items)."
        )
    if outcome == "interrupted":
        found.append("The turn has no end event: the engine stopped or crashed mid-turn.")
    if failed_tools:
        found.append(f"{failed_tools} tool call(s) failed.")
    if turn.approvals_requested > turn.approvals_resolved:
        found.append("An approval was requested and never resolved.")
    if turn.loop_detected:
        found.append("The loop detector intervened.")
    if any(c.get("status") == "failed" for c in turn.compactions):
        found.append("A context compaction failed.")
    return found


def _end_status(kind: str, payload: dict) -> str:
    if kind == "turn.failed":
        return "failed"
    if kind in ("turn.cancelled", "turn.stopped", "TurnInterrupted"):
        return "cancelled"
    if kind == "turn.completed" and not payload.get("content"):
        return "completed_empty"
    return "completed"


def _event_view(event) -> dict[str, Any]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    keep = {}
    for key, value in payload.items():
        if key in ("workspace_root", "session_id", "turn_id", "activity_seq", "occurred_at"):
            continue
        keep[key] = _clip(value, 200) if isinstance(value, (dict, list, str)) else value
    return {"seq": event.seq, "type": event.type, "at": event.created_at, "data": keep}


__all__ = ["Introspection"]
