"""`/usage` (plan 0.4.0 #78): the usage report the desktop paints in the chat.

Built from what the Engine already stores: one ``ModelInvoked`` per model
call (with its usage and the model in ``context_anchor``), ``ToolCompleted``
per tool, ``VisualPixelsDelivered`` per image the model saw, the governor's
compactions and loop events, and the turns' start and end. No model call.

A report can be saved in its conversation as a snapshot
(``usage.reported``, never sent to the model) and listed again.
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.shared.clock import now_iso

SCOPES = ("session", "today", "project", "all", "agent")
REPORT_EVENT = "usage.reported"
#: A conversation keeps its latest snapshots, not an unbounded list.
MAX_SAVED = 50

_TERMINAL = ("turn.completed", "turn.failed", "turn.cancelled", "turn.stopped")


def register_usage_report(dispatcher: Any, services: Any) -> None:
    from rinari.providers.usage import ProviderUsageService

    usage_service = ProviderUsageService(services.providers)

    def context_of(session_id: str) -> dict[str, Any] | None:
        status = dispatcher.handler("context.status")
        return status({"session_id": session_id}) if status else None

    dispatcher.register(
        "usage.report",
        lambda params: usage_report(
            services, params or {}, limits_of=usage_service.get, context_of=context_of
        ),
    )
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
    if scope == "agent":
        # Rinari Agency: what an agent spent today, over its conversations.
        if not ref:
            raise EngineProtocolError(INVALID_PARAMS, "An agent report needs 'ref'.")
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            list(services.ctx.agency_repo.session_ids(ref)),
            start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        )
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


def build_report(
    services: Any,
    scope: str,
    ref: str | None,
    *,
    limits_of: Any = None,
    context_of: Any = None,
    app_version: str | None = None,
) -> dict[str, Any]:
    if scope not in SCOPES:
        raise EngineProtocolError(
            INVALID_PARAMS, f"Param 'scope' must be one of: {', '.join(SCOPES)}."
        )
    sessions, since = _sessions(services, scope, ref)
    # An agent without conversations spent nothing (never «every session»).
    if sessions == [] and scope != "agent":
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

    from rinari.runtime import tool_events

    tools: Counter[str] = Counter()
    failed = 0
    ended = [
        (row["type"], payload)
        for row, payload in _events(services, tool_events.ENDED, sessions, since)
    ]
    for end in tool_events.ends(ended):
        tools[str(end["tool"])] += 1
        if not end["ok"]:
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
    if scope == "session" and sessions:
        large = _large_outputs_in_context(services, sessions[0])
        if large:
            warnings.append({"kind": "large_outputs", "count": len(large), "max": max(large)})

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
    # Images created with a connector (plan 0.4.0 #28): their cost joins the total.
    media = {"images": 0, "batches": 0, "cost": None, "by_connector": {}}
    for _row, payload in _events(services, ("media.generated",), sessions, since):
        media["batches"] += 1
        media["images"] += int(payload.get("images") or 0)
        connector = str(payload.get("connector") or "unknown")
        entry = media["by_connector"].setdefault(
            connector, {"connector": connector, "images": 0, "cost": None}
        )
        entry["images"] += int(payload.get("images") or 0)
        cost = payload.get("cost_usd")
        if isinstance(cost, (int, float)):
            entry["cost"] = round((entry["cost"] or 0) + float(cost), 4)
            media["cost"] = round((media["cost"] or 0) + float(cost), 4)
    media["by_connector"] = list(media["by_connector"].values())
    if media["cost"]:
        costs.append(media["cost"])
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
        "media": media,
        "warnings": warnings,
        "time": {"first_at": first_at, "last_at": last_at, "active_ms": active_ms},
        **_snapshot_extras(
            services,
            scope,
            ref,
            rows,
            limits_of=limits_of,
            context_of=context_of,
            app_version=app_version,
        ),
    }


def _snapshot_extras(
    services: Any,
    scope: str,
    ref: str | None,
    rows: list[dict[str, Any]],
    *,
    limits_of: Any,
    context_of: Any,
    app_version: str | None,
) -> dict[str, Any]:
    """What the report says besides the counts: who, the context and the plan's
    limits, taken now so the saved snapshot keeps them (#78)."""
    from rinari import __version__

    record = None
    if ref and scope in ("session", "project"):
        try:
            record = services.sessions.show(ref)
        except Exception:
            record = None
    provider_id = getattr(record, "provider_id", None)
    model_id = getattr(record, "model_id", None)
    if record is None:
        selection = services.providers.current()
        provider_id = selection.provider.id if selection else None
        model_id = selection.model.id if selection and selection.model else None
    client = {
        "app_version": app_version,
        "engine_version": __version__,
        "profile": _profile_name(services, getattr(record, "rinari_profile_id", None)),
        "provider": _alias(services.ctx.provider_repo, provider_id),
        "model": _alias(services.ctx.model_repo, model_id),
    }
    context = None
    if record is not None and scope == "session" and context_of is not None:
        try:
            status = context_of(record.id) or {}
        except Exception:
            status = {}
        if status.get("window_tokens"):
            last = status.get("last_request") or {}
            compaction = status.get("last_compaction") or {}
            context = {
                "window_tokens": status["window_tokens"],
                "used_tokens": last.get("input_tokens"),
                "compact_at_tokens": status.get("compact_at_tokens"),
                "last_compaction_at": compaction.get("at"),
            }
    return {
        "client": client,
        "context": context,
        "limits": _limits(services, provider_id, rows, limits_of),
    }


def _alias(repo: Any, item_id: str | None) -> str | None:
    if not item_id:
        return None
    try:
        item = repo.get(item_id)
    except Exception:
        return None
    return getattr(item, "alias", None) or item_id


def _profile_name(services: Any, profile_id: str | None) -> str | None:
    profiles = getattr(services, "rinari_profiles", None)
    if profiles is None:
        return None
    try:
        bundle = profiles.store.get(profile_id or profiles.active_id())
    except Exception:
        return None
    return getattr(bundle, "name", None)


#: Statuses worth showing for a provider other than the conversation's.
_TELLING = ("available", "stale", "needs_auth")


def _limits(
    services: Any, provider_id: str | None, rows: list[dict[str, Any]], limits_of: Any
) -> list[dict[str, Any]]:
    """The plan limits of the conversation's provider first, then of every other
    connected one that reports them (#78). One failing does not hide the rest."""
    if limits_of is None:
        return []
    used = [row.get("provider_id") for row in rows if row.get("provider_id")]
    order: list[str] = [item for item in [provider_id, *used] if item]
    for provider in services.providers.list():
        if getattr(provider, "enabled", True):
            order.append(provider.id)
    seen: set[str] = set()
    limits = []
    for index, pid in enumerate(order):
        if pid in seen:
            continue
        seen.add(pid)
        try:
            snapshot = limits_of(pid)
        except Exception:
            continue
        own = index == 0 and pid == provider_id
        if not own and snapshot.get("status") not in _TELLING:
            continue
        limits.append(
            {
                "provider_id": pid,
                "provider_alias": _alias(services.ctx.provider_repo, pid),
                "product_id": snapshot.get("product_id"),
                "status": snapshot.get("status"),
                "windows": snapshot.get("windows") or [],
                "balances": snapshot.get("balances") or [],
                "detail": snapshot.get("detail"),
                "fetched_at": snapshot.get("fetched_at"),
                "conversation": own,
            }
        )
    return limits


#: A tool output this long (characters, about 5k tokens) weighs on every call.
LARGE_OUTPUT_CHARS = 20_000


def _large_outputs_in_context(services: Any, session_id: str) -> list[int]:
    """Sizes of the large tool outputs the model still carries: those after the
    last compaction (#78). Compacting now drops them."""
    last = services.ctx.db.query_one(
        "SELECT MAX(seq) AS seq FROM session_events WHERE session_id = ? AND type = ?",
        (session_id, "governor.compact"),
    )
    rows = services.ctx.db.query(
        "SELECT length(json_extract(payload_json, '$.observation')) AS size "
        "FROM session_events WHERE session_id = ? AND type = 'tool.completed' AND seq > ? "
        "AND length(json_extract(payload_json, '$.observation')) > ?",
        (session_id, (last["seq"] if last else None) or 0, LARGE_OUTPUT_CHARS),
    )
    return [int(row["size"]) for row in rows]


def _name_model(services: Any, entry: dict[str, Any]) -> None:
    try:
        model = services.ctx.model_repo.get(entry["model_id"])
    except Exception:
        model = None
    entry["model_name"] = (
        getattr(model, "alias", None) or getattr(model, "model_name", None) or entry["model_id"]
    )
    entry["_settings"] = getattr(model, "settings", None) or {}
    entry["_provider_model_id"] = getattr(model, "provider_model_id", None)
    provider = None
    if entry.get("provider_id"):
        try:
            provider = services.ctx.provider_repo.get(entry["provider_id"])
        except Exception:
            provider = None
    entry["provider_alias"] = getattr(provider, "alias", None)
    entry["_provider"] = provider


def _prices(entry: dict[str, Any]) -> dict[str, float] | None:
    """USD per million tokens: the model's own prices, else the public
    catalog's (models.dev, in model_metadata.json)."""
    settings = entry.pop("_settings", {}) or {}
    provider = entry.pop("_provider", None)
    provider_model_id = entry.pop("_provider_model_id", None)
    try:
        own = {
            "input": float(settings["input_price_per_mtok"]),
            "output": float(settings["output_price_per_mtok"]),
        }
    except (KeyError, TypeError, ValueError):
        own = None
    if own is not None:
        for key in ("cache_read", "cache_write"):
            with contextlib.suppress(KeyError, TypeError, ValueError):
                own[key] = float(settings[f"{key}_price_per_mtok"])
        return own
    if provider is None or not provider_model_id:
        return None
    from rinari.providers.metadata import catalog_prices

    try:
        return catalog_prices(provider, provider_model_id)
    except Exception:
        return None


def _cost(services: Any, entry: dict[str, Any]) -> float | None:
    """Cost when a price is known. Input counts everything the model read, so
    the cached part is taken out and priced at the cache's rates (the input
    rate when the cache's is not known: an upper bound)."""
    prices = _prices(entry)
    if prices is None:
        return None
    read = entry["cache_read_tokens"]
    written = entry["cache_write_tokens"]
    fresh = max(0, entry["input_tokens"] - read - written)
    total = (
        fresh * prices["input"]
        + read * prices.get("cache_read", prices["input"])
        + written * prices.get("cache_write", prices["input"])
        + entry["output_tokens"] * prices["output"]
    )
    return round(total / 1_000_000, 4)


def _ms_between(start: str, end: str) -> int:
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, int((b - a).total_seconds() * 1000))


def usage_report(
    services: Any,
    params: dict[str, Any],
    *,
    limits_of: Any = None,
    context_of: Any = None,
) -> dict[str, Any]:
    scope = params.get("scope", "session")
    ref = params.get("ref")
    if ref is not None and not isinstance(ref, str):
        raise EngineProtocolError(INVALID_PARAMS, "Param 'ref' must be a string.")
    app_version = params.get("app_version")
    if app_version is not None and not isinstance(app_version, str):
        raise EngineProtocolError(INVALID_PARAMS, "Param 'app_version' must be a string.")
    report = build_report(
        services,
        scope,
        ref,
        limits_of=limits_of,
        context_of=context_of,
        app_version=app_version,
    )
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


def _n(value: Any) -> str:
    return f"{_int(value):,}"


def render_text(report: dict[str, Any]) -> str:
    """The report as text: what `/usage` prints in the CLI and what the desktop
    copies (`Plan limits:` / `Context:` / `Session:` ...)."""
    client = report.get("client") or {}
    lines = [f"Usage report · {report.get('scope')} · {report.get('generated_at')}"]
    who = [
        f"Rinari Agent {client['app_version']}" if client.get("app_version") else None,
        f"Engine {client['engine_version']}" if client.get("engine_version") else None,
        f"profile {client['profile']}" if client.get("profile") else None,
        " / ".join(item for item in (client.get("provider"), client.get("model")) if item) or None,
    ]
    if any(who):
        lines.append(" · ".join(item for item in who if item))
    limits = report.get("limits") or []
    if limits:
        lines += ["", "Plan limits:"]
        for item in limits:
            name = item.get("provider_alias") or item.get("provider_id")
            windows = item.get("windows") or []
            balances = item.get("balances") or []
            if not windows and not balances:
                lines.append(f"  {name}: not reported ({item.get('detail') or item.get('status')})")
                continue
            for window in windows:
                used = window.get("used_percent")
                text = f"{round(used)}% used" if isinstance(used, (int, float)) else "unknown"
                resets = f" · resets {window['resets_at']}" if window.get("resets_at") else ""
                lines.append(
                    f"  {name} · {window.get('label') or window.get('id')}: {text}{resets}"
                )
            for balance in balances:
                remaining = "unlimited" if balance.get("unlimited") else balance.get("remaining")
                currency = balance.get("currency") or ""
                lines.append(f"  {name} · {balance.get('label')}: {remaining} {currency}".rstrip())
    context = report.get("context")
    if context and context.get("window_tokens"):
        used = context.get("used_tokens")
        share = f" ({round(100 * used / context['window_tokens'])}%)" if used else ""
        lines += [
            "",
            "Context:",
            f"  {_n(used) if used else '—'} of {_n(context['window_tokens'])} tokens{share}"
            + f" · {report.get('compactions', 0)} compactions",
        ]
    lines += ["", "Session:"]
    for row in report.get("models") or []:
        cost = f" · ${row['cost']:.4f}" if row.get("cost") is not None else ""
        reasoning = (
            f" · {_n(row['reasoning_tokens'])} reasoning" if row.get("reasoning_tokens") else ""
        )
        lines.append(
            f"  {row.get('model_name')}: {row.get('calls')} calls"
            f" · {_n(row.get('input_tokens'))} input · {_n(row.get('output_tokens'))} output"
            f" · {_n(row.get('cache_read_tokens'))} cache read"
            f" · {_n(row.get('cache_write_tokens'))} cache write{reasoning}{cost}"
        )
    totals = report.get("totals") or {}
    total_cost = f" · ${totals['cost']:.4f}" if totals.get("cost") is not None else ""
    lines.append(
        f"  Total: {totals.get('calls', 0)} calls · {_n(totals.get('input_tokens'))} input"
        f" · {_n(totals.get('output_tokens'))} output{total_cost}"
    )
    tools = report.get("tools") or {}
    time_ = report.get("time") or {}
    top = ", ".join(f"{item['tool']} {item['count']}" for item in tools.get("top") or [])
    lines += [
        "",
        f"Tools: {tools.get('total', 0)} ({tools.get('ok', 0)} ok, {tools.get('failed', 0)} failed)"
        + (f" · {top}" if top else ""),
        f"Compactions: {report.get('compactions', 0)}",
        *(
            [
                f"Images: {media.get('images', 0)}"
                + (f" · ${media['cost']:.4f}" if media.get("cost") else "")
            ]
            if (media := report.get("media") or {}).get("images")
            else []
        ),
        f"Active time: {round(time_.get('active_ms', 0) / 1000)} s"
        + (
            f" · from {time_['first_at']} to {time_['last_at']}"
            if time_.get("first_at") and time_.get("last_at")
            else ""
        ),
    ]
    warnings = report.get("warnings") or []
    if warnings:
        lines += ["", "Warnings:", *(f"  - {item['kind']}: {item['count']}" for item in warnings)]
    return "\n".join(lines) + "\n"
