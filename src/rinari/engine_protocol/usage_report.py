"""`/usage` (plan 0.4.0 #78): the usage report the desktop paints in the chat.

Built from what the Engine already stores: one ``ModelInvoked`` per model
call (with its usage and the model in ``context_anchor``), ``ToolCompleted``
per tool, ``VisualPixelsDelivered`` per image the model saw, the governor's
compactions and loop events, and the turns' start and end. No model call.

A report can be saved in its conversation as a snapshot
(``usage.reported``, never sent to the model) and listed again.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.shared.clock import now_iso

SCOPES = ("session", "today", "project", "all")
REPORT_EVENT = "usage.reported"
#: A conversation keeps its latest snapshots, not an unbounded list.
MAX_SAVED = 50

_TERMINAL = ("turn.completed", "turn.failed", "turn.cancelled", "turn.stopped")


def register_usage_report(dispatcher: Any, services: Any) -> None:
    dispatcher.register("usage.report", lambda params: usage_report(services, params or {}))
    dispatcher.register("usage.report.list", lambda params: report_list(services, params or {}))


def _sessions(services: Any, scope: str, ref: str | None) -> tuple[list[str] | None, str | None]:
    """The sessions a scope covers (None = every one) and since when."""
    if scope == "session":
        if not ref:
            raise EngineProtocolError(INVALID_PARAMS, "A conversation report needs 'ref'.")
        return [services.sessions.show(ref).id], None
    if scope == "today":
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return None, start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    if scope == "project":
        if not ref:
            raise EngineProtocolError(INVALID_PARAMS, "A project report needs 'ref'.")
        record = services.sessions.show(ref)
        if not record.project_id:
            return [record.id], None
        rows = services.ctx.db.query(
            "SELECT id FROM sessions WHERE project_id = ?", (record.project_id,)
        )
        return [str(row["id"]) for row in rows], None
    return None, None


def _events(services: Any, types: tuple[str, ...], sessions: list[str] | None, since: str | None):
    marks = ", ".join("?" for _ in types)
    sql = (
        "SELECT session_id, type, payload_json, created_at, turn_id FROM session_events "
        f"WHERE type IN ({marks})"
    )
    params: list[Any] = list(types)
    if sessions is not None:
        sql += " AND session_id IN ({})".format(", ".join("?" for _ in sessions) or "''")
        params += sessions
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    sql += " ORDER BY seq ASC"
    for row in services.ctx.db.query(sql, params):
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            payload = {}
        yield row, payload


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def build_report(services: Any, scope: str, ref: str | None) -> dict[str, Any]:
    if scope not in SCOPES:
        raise EngineProtocolError(
            INVALID_PARAMS, f"Param 'scope' must be one of: {', '.join(SCOPES)}."
        )
    sessions, since = _sessions(services, scope, ref)
    if sessions == []:
        sessions = None

    models: dict[str, dict[str, Any]] = {}
    empty = 0
    no_cache: Counter[str] = Counter()
    calls_by_model: Counter[str] = Counter()
    for _row, payload in _events(services, ("ModelInvoked",), sessions, since):
        anchor = payload.get("context_anchor") or {}
        model_id = str(anchor.get("model") or payload.get("model_id") or "unknown")
        usage = payload.get("usage") or {}
        entry = models.setdefault(
            model_id,
            {
                "model_id": model_id,
                "provider_id": payload.get("provider_id"),
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
            },
        )
        entry["calls"] += 1
        entry["input_tokens"] += _int(usage.get("input_tokens"))
        entry["output_tokens"] += _int(usage.get("output_tokens"))
        entry["cache_read_tokens"] += _int(usage.get("cached_input_tokens"))
        entry["cache_write_tokens"] += _int(usage.get("cache_write_tokens"))
        entry["reasoning_tokens"] += _int(usage.get("reasoning_tokens"))
        calls_by_model[model_id] += 1
        if not payload.get("tool_calls") and _int(usage.get("output_tokens")) == 0:
            empty += 1
        if _int(usage.get("input_tokens")) > 20_000 and _int(usage.get("cached_input_tokens")) == 0:
            no_cache[model_id] += 1

    for entry in models.values():
        _name_model(services, entry)
        entry["cache_percent"] = (
            round(100 * entry["cache_read_tokens"] / entry["input_tokens"])
            if entry["input_tokens"]
            else None
        )
        entry["cost"] = _cost(services, entry)

    tools: Counter[str] = Counter()
    failed = 0
    for _row, payload in _events(services, ("ToolCompleted", "ToolFailed"), sessions, since):
        name = str(payload.get("tool") or payload.get("name") or "tool")
        tools[name] += 1
        if payload.get("ok") is False:
            failed += 1
    total_tools = sum(tools.values())

    image_counts: Counter[str] = Counter()
    for _row, payload in _events(services, ("VisualPixelsDelivered",), sessions, since):
        for source in payload.get("sources") or []:
            for image in source.get("images") or []:
                if image.get("sha256"):
                    image_counts[str(image["sha256"])] += 1

    compactions = sum(1 for _ in _events(services, ("governor.compact",), sessions, since))
    loops = sum(1 for _ in _events(services, ("LoopDetected", "governor.stop"), sessions, since))

    starts: dict[str, str] = {}
    active_ms = 0
    first_at = last_at = None
    for row, payload in _events(services, ("turn.started", *_TERMINAL), sessions, since):
        at = str(payload.get("occurred_at") or row["created_at"])
        first_at = first_at or at
        last_at = at
        if row["type"] == "turn.started":
            starts[str(row["turn_id"])] = at
        elif str(row["turn_id"]) in starts:
            active_ms += _ms_between(starts.pop(str(row["turn_id"])), at)

    warnings = []
    resent = [(sha, count) for sha, count in image_counts.items() if count >= 3]
    if resent:
        warnings.append(
            {"kind": "image_resent", "count": len(resent), "max": max(count for _, count in resent)}
        )
    if empty:
        warnings.append({"kind": "empty_answers", "count": empty})
    for model_id, count in no_cache.items():
        if count >= 5 and count >= calls_by_model[model_id] // 2:
            warnings.append(
                {"kind": "no_cache", "count": count, "model": models[model_id].get("model_name")}
            )
    if loops:
        warnings.append({"kind": "loops", "count": loops})

    rows = sorted(models.values(), key=lambda entry: entry["calls"], reverse=True)
    totals = {
        key: sum(entry[key] for entry in rows)
        for key in (
            "calls",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        )
    }
    costs = [entry["cost"] for entry in rows if entry["cost"] is not None]
    totals["cost"] = round(sum(costs), 4) if costs else None
    return {
        "scope": scope,
        "ref": ref,
        "generated_at": now_iso(services.ctx.clock),
        "models": rows,
        "totals": totals,
        "tools": {
            "total": total_tools,
            "ok": total_tools - failed,
            "failed": failed,
            "top": [{"tool": name, "count": count} for name, count in tools.most_common(5)],
        },
        "compactions": compactions,
        "warnings": warnings,
        "time": {"first_at": first_at, "last_at": last_at, "active_ms": active_ms},
    }


def _name_model(services: Any, entry: dict[str, Any]) -> None:
    try:
        model = services.ctx.model_repo.get(entry["model_id"])
    except Exception:
        model = None
    entry["model_name"] = (
        getattr(model, "alias", None) or getattr(model, "model_name", None) or entry["model_id"]
    )
    entry["_settings"] = getattr(model, "settings", None) or {}
    provider = None
    if entry.get("provider_id"):
        try:
            provider = services.ctx.provider_repo.get(entry["provider_id"])
        except Exception:
            provider = None
    entry["provider_alias"] = getattr(provider, "alias", None)


def _cost(services: Any, entry: dict[str, Any]) -> float | None:
    """Cost only when the model has prices configured (USD per million tokens)."""
    settings = entry.pop("_settings", {}) or {}
    try:
        price_in = float(settings["input_price_per_mtok"])
        price_out = float(settings["output_price_per_mtok"])
    except (KeyError, TypeError, ValueError):
        return None
    billed_in = max(0, entry["input_tokens"] - entry["cache_read_tokens"])
    return round((billed_in * price_in + entry["output_tokens"] * price_out) / 1_000_000, 4)


def _ms_between(start: str, end: str) -> int:
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, int((b - a).total_seconds() * 1000))


def usage_report(services: Any, params: dict[str, Any]) -> dict[str, Any]:
    scope = params.get("scope", "session")
    ref = params.get("ref")
    if ref is not None and not isinstance(ref, str):
        raise EngineProtocolError(INVALID_PARAMS, "Param 'ref' must be a string.")
    report = build_report(services, scope, ref)
    if params.get("save"):
        # The snapshot stays in its conversation, never sent to the model.
        session_id = params.get("session_id") or ref
        if not isinstance(session_id, str) or not session_id:
            raise EngineProtocolError(INVALID_PARAMS, "Saving a report needs 'session_id'.")
        record = services.sessions.show(session_id)
        _save(services, record.id, report)
    return {"report": report}


def _save(services: Any, session_id: str, report: dict[str, Any]) -> None:
    from rinari.storage.records import SessionEventRecord

    repo = services.ctx.event_repo
    repo.insert(
        SessionEventRecord(
            id=services.ctx.ids.new("evt"),
            session_id=session_id,
            seq=repo.next_seq(session_id),
            type=REPORT_EVENT,
            payload={"report": report},
            created_at=report["generated_at"],
        )
    )
    old = services.ctx.db.query(
        "SELECT id FROM session_events WHERE session_id = ? AND type = ? "
        "ORDER BY seq DESC LIMIT -1 OFFSET ?",
        (session_id, REPORT_EVENT, MAX_SAVED),
    )
    for row in old:
        services.ctx.db.execute("DELETE FROM session_events WHERE id = ?", (row["id"],))


def report_list(services: Any, params: dict[str, Any]) -> dict[str, Any]:
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise EngineProtocolError(INVALID_PARAMS, "Param 'session_id' is required.")
    record = services.sessions.show(session_id)
    rows = services.ctx.event_repo.list(record.id, types=[REPORT_EVENT])
    return {"reports": [dict(row.payload.get("report") or {}) for row in rows]}
