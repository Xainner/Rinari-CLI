"""`rinari metrics` group: aggregate runtime metrics from stored events
(commands.md 52). All numbers come from persisted session events - nothing
is estimated or invented.
"""

from __future__ import annotations

from datetime import datetime

import typer

from rinari.cli.deps import is_json, services, with_error_handling

from ..output import emit_json, success_envelope

app = typer.Typer(help="Runtime metrics from stored events.", no_args_is_help=True)


def _all_events(s):
    events = []
    for record in s.ctx.session_repo.list():
        events.extend(s.ctx.event_repo.list(record.id))
    return events


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


@app.command("metrics")
@with_error_handling("metrics")
def metrics_all(
    ctx: typer.Context,
    session: str = typer.Option(None, "--session", help="Restrict to one session."),
) -> None:
    """Show all metric groups at once."""
    with services(ctx) as s:
        events = _select(s, session)
        sessions_rows = _sessions_metrics(s, session)
        data = {
            "scope": session or "all sessions",
            "sessions": sessions_rows,
            "model": _model_metrics(events),
            "tools": _tool_metrics(events),
            "turns": _turn_metrics(events),
            "approvals": _approval_metrics(events),
            "completion": _completion_metrics(events),
        }
        if is_json(ctx):
            emit_json(success_envelope("metrics", data))
            return
        _print(data)


def _select(s, session: str | None):
    if session:
        record = s.ctx.session_repo.get(session)
        if record is None:
            from rinari.shared.errors import NotFoundError

            raise NotFoundError(f"Session not found: {session}")
        return s.ctx.event_repo.list(record.id)
    return _all_events(s)


def _sessions_metrics(s, session: str | None):
    records = s.ctx.session_repo.list()
    if session:
        records = [r for r in records if r.id == session]
    by_kind: dict[str, int] = {}
    by_state: dict[str, int] = {}
    for r in records:
        by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
        by_state[r.state] = by_state.get(r.state, 0) + 1
    return {"total": len(records), "by_kind": by_kind, "by_state": by_state}


def _model_metrics(events):
    calls = 0
    tokens = {"input": 0, "output": 0, "cached": 0, "reasoning": 0}
    stop_reasons: dict[str, int] = {}
    for e in events:
        if e.type != "ModelInvoked":
            continue
        calls += 1
        usage = (e.payload or {}).get("usage") or {}
        tokens["input"] += usage.get("input_tokens") or 0
        tokens["output"] += usage.get("output_tokens") or 0
        tokens["cached"] += usage.get("cached_input_tokens") or 0
        tokens["reasoning"] += usage.get("reasoning_tokens") or 0
        reason = (e.payload or {}).get("stop_reason")
        if reason:
            stop_reasons[reason] = stop_reasons.get(reason, 0) + 1
    return {
        "calls": calls,
        "tokens": tokens,
        "stop_reasons": stop_reasons,
        "cost": None,  # only derivable with reliable pricing; never invented
    }


def _tool_metrics(events):
    by_name: dict[str, dict] = {}
    total_ok = 0
    total_err = 0
    durations = []
    for e in events:
        if e.type != "ToolCompleted":
            continue
        payload = e.payload or {}
        name = payload.get("name", "?")
        ok = bool(payload.get("ok"))
        entry = by_name.setdefault(name, {"calls": 0, "errors": 0, "duration_ms_sum": 0.0})
        entry["calls"] += 1
        if ok:
            total_ok += 1
        else:
            total_err += 1
            entry["errors"] += 1
        d = payload.get("duration_ms")
        if isinstance(d, (int, float)):
            entry["duration_ms_sum"] += d
            durations.append(d)
    tools = []
    for name, entry in sorted(by_name.items()):
        tools.append(
            {
                "name": name,
                "calls": entry["calls"],
                "errors": entry["errors"],
                "avg_duration_ms": round(entry["duration_ms_sum"] / entry["calls"], 1),
            }
        )
    return {
        "calls": total_ok + total_err,
        "ok": total_ok,
        "errors": total_err,
        "tools": tools,
    }


def _turn_metrics(events):
    started: list[str] = []
    durations_s = []
    kinds: dict[str, int] = {}
    loops = 0
    compactions = 0
    for e in events:
        if e.type == "AgentTurnStarted":
            started.append(e.created_at)
        elif e.type == "AgentTurnCompleted":
            kind = (e.payload or {}).get("kind", "?")
            kinds[kind] = kinds.get(kind, 0) + 1
            if started:
                a, b = _parse_ts(started.pop(0)), _parse_ts(e.created_at)
                if a is not None and b is not None:
                    durations_s.append((b - a).total_seconds())
        elif e.type == "LoopDetected":
            loops += 1
        elif e.type == "ContextCompacted":
            compactions += 1
    avg = round(sum(durations_s) / len(durations_s), 2) if durations_s else None
    return {
        "turns": sum(kinds.values()),
        "kinds": kinds,
        "avg_duration_s": avg,
        "loops": loops,
        "compactions": compactions,
    }


def _approval_metrics(events):
    requested = approved = denied = 0
    for e in events:
        if e.type == "PermissionRequest":
            requested += 1
        elif e.type == "ToolApproved":
            approved += 1
        elif e.type == "ApprovalDenied":
            denied += 1
    return {"requested": requested, "approved": approved, "denied": denied}


def _completion_metrics(events):
    outcomes: dict[str, int] = {}
    for e in events:
        if e.type == "CompletionGateEvaluated":
            outcome = (e.payload or {}).get("outcome", "?")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {"evaluations": sum(outcomes.values()), "outcomes": outcomes}


def _print(data: dict) -> None:
    sess = data["sessions"]
    typer.echo(f"scope: {data['scope']}")
    typer.echo(
        f"sessions:  {sess['total']}  by_kind={sess['by_kind']}  by_state={sess['by_state']}"
    )
    model = data["model"]
    typer.echo(f"model:     {model['calls']} calls  tokens={model['tokens']}  cost=unknown")
    tools = data["tools"]
    typer.echo(f"tools:     {tools['calls']} calls  ok={tools['ok']}  errors={tools['errors']}")
    turns = data["turns"]
    typer.echo(
        f"turns:     {turns['turns']}  kinds={turns['kinds']}  avg={turns['avg_duration_s']}s "
        f"loops={turns['loops']}  compactions={turns['compactions']}"
    )
    appr = data["approvals"]
    typer.echo(
        f"approvals: requested={appr['requested']}  "
        f"approved={appr['approved']}  denied={appr['denied']}"
    )
    comp = data["completion"]
    typer.echo(f"completion: {comp['evaluations']} evaluations  outcomes={comp['outcomes']}")
