"""`rinari metrics` group: aggregate runtime metrics from stored events
(commands.md 52). All numbers come from persisted session events - nothing
is estimated or invented; metrics without a source display `unknown`.
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
    durations: list[float] = []
    for e in events:
        if e.type not in ("ToolCompleted", "ToolFailed") or "tool_call_id" not in (e.payload or {}):
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
        "durations_ms": durations,
    }


def _turn_metrics(events):
    started: list[str] = []
    durations_s: list[float] = []
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
        "durations_s": durations_s,
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


def _skill_metrics(events):
    activated: dict[str, int] = {}
    deactivated = 0
    for e in events:
        if e.type == "SkillActivated":
            name = (e.payload or {}).get("skill", "?")
            activated[name] = activated.get(name, 0) + 1
        elif e.type == "SkillDeactivated":
            deactivated += 1
    return {"activations": len(activated), "skills": activated, "deactivations": deactivated}


def _agent_metrics(events):
    starts: dict[str, int] = {}
    stops: dict[str, int] = {}
    for e in events:
        name = (e.payload or {}).get("agent") or (e.payload or {}).get("agent_id") or "?"
        if e.type == "SubagentStart":
            starts[name] = starts.get(name, 0) + 1
        elif e.type == "SubagentStop":
            stops[name] = stops.get(name, 0) + 1
    keys = sorted(set(starts) | set(stops))
    return {
        "spawned": sum(starts.values()),
        "stopped": sum(stops.values()),
        "by_agent": {k: {"started": starts.get(k, 0), "stopped": stops.get(k, 0)} for k in keys},
    }


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((pct / 100.0) * (len(ordered) - 1)))
    return round(ordered[index], 1)


def _latency_metrics(events):
    tool_durations: list[float] = []
    turn_durations: list[float] = []
    started: list[str] = []
    for e in events:
        if e.type in ("ToolCompleted", "ToolFailed") and "tool_call_id" in (e.payload or {}):
            d = (e.payload or {}).get("duration_ms")
            if isinstance(d, (int, float)):
                tool_durations.append(d)
        elif e.type == "AgentTurnStarted":
            started.append(e.created_at)
        elif e.type == "AgentTurnCompleted":
            if started:
                a, b = _parse_ts(started.pop(0)), _parse_ts(e.created_at)
                if a is not None and b is not None:
                    turn_durations.append((b - a).total_seconds())
    return {
        "tools_ms": {
            "count": len(tool_durations),
            "p50": _percentile(tool_durations, 50),
            "p95": _percentile(tool_durations, 95),
            "max": round(max(tool_durations), 1) if tool_durations else None,
        },
        "turns_s": {
            "count": len(turn_durations),
            "avg": round(sum(turn_durations) / len(turn_durations), 2) if turn_durations else None,
            "p95": _percentile(turn_durations, 95),
        },
        "model_s": None,  # model call wall-time is not traced; never invented
    }


def _success_metrics(events):
    turns = _turn_metrics(events)
    completion = _completion_metrics(events)
    approvals = _approval_metrics(events)
    tool = _tool_metrics(events)
    verified = completion["outcomes"].get("VERIFIED", 0)
    gate_rate = (
        round(verified / completion["evaluations"], 3) if completion["evaluations"] else None
    )
    loop_rate = round(turns["loops"] / turns["turns"], 3) if turns["turns"] else None
    tool_error_rate = round(tool["errors"] / tool["calls"], 3) if tool["calls"] else None
    return {
        "turns": turns["turns"],
        "turn_kinds": turns["kinds"],
        "task_success": {
            "verified_gate_evaluations": verified,
            "gate_evaluations": completion["evaluations"],
            "rate": gate_rate,  # None when no gate evaluations exist
            "outcomes": completion["outcomes"],
        },
        "human_intervention": {
            "approval_requests": approvals["requested"],
            "approved": approvals["approved"],
            "denied": approvals["denied"],
        },
        "loop_rate": loop_rate,
        "tool_error_rate": tool_error_rate,
        "avg_turn_duration_s": turns["avg_duration_s"],
        "compactions": turns["compactions"],
    }


def _emit(ctx: typer.Context, command: str, data: dict) -> None:
    if is_json(ctx):
        emit_json(success_envelope(command, data))


# --- commands -----------------------------------------------------------------


@app.command("all")
@with_error_handling("metrics.all")
def metrics_all(
    ctx: typer.Context,
    session: str = typer.Option(None, "--session", help="Restrict to one session."),
) -> None:
    """Show all metric groups at once."""
    with services(ctx) as s:
        events = _select(s, session)
        data = {
            "scope": session or "all sessions",
            "sessions": _sessions_metrics(s, session),
            "model": _model_metrics(events),
            "tools": _tool_metrics(events),
            "turns": _turn_metrics(events),
            "approvals": _approval_metrics(events),
            "completion": _completion_metrics(events),
            "skills": _skill_metrics(events),
            "agents": _agent_metrics(events),
        }
    _emit(ctx, "metrics.all", data)
    if not is_json(ctx):
        _print_all(data)


@app.command("sessions")
@with_error_handling("metrics.sessions")
def metrics_sessions(
    ctx: typer.Context,
    session: str = typer.Option(None, "--session", help="Restrict to one session."),
) -> None:
    """Session counts by kind and state."""
    with services(ctx) as s:
        data = _sessions_metrics(s, session)
    _emit(ctx, "metrics.sessions", data)
    if not is_json(ctx):
        typer.echo(
            f"sessions: {data['total']}  by_kind={data['by_kind']}  by_state={data['by_state']}"
        )


@app.command("models")
@with_error_handling("metrics.models")
def metrics_models(
    ctx: typer.Context,
    session: str = typer.Option(None, "--session", help="Restrict to one session."),
) -> None:
    """Model calls, token usage and stop reasons per session."""
    with services(ctx) as s:
        data = _model_metrics(_select(s, session))
    _emit(ctx, "metrics.models", data)
    if not is_json(ctx):
        typer.echo(f"model calls: {data['calls']}  tokens={data['tokens']}  cost=unknown")


@app.command("tools")
@with_error_handling("metrics.tools")
def metrics_tools(
    ctx: typer.Context,
    session: str = typer.Option(None, "--session", help="Restrict to one session."),
) -> None:
    """Tool call volume and error rate per tool."""
    with services(ctx) as s:
        data = _tool_metrics(_select(s, session))
    data.pop("durations_ms", None)
    _emit(ctx, "metrics.tools", data)
    if not is_json(ctx):
        typer.echo(f"tools: {data['calls']} calls  ok={data['ok']}  errors={data['errors']}")
        for row in data["tools"][:15]:
            typer.echo(
                f"  {row['name']}: {row['calls']} calls  {row['errors']} errors  "
                f"avg {row['avg_duration_ms']} ms"
            )


@app.command("skills")
@with_error_handling("metrics.skills")
def metrics_skills(
    ctx: typer.Context,
    session: str = typer.Option(None, "--session", help="Restrict to one session."),
) -> None:
    """Skill activation counts."""
    with services(ctx) as s:
        data = _skill_metrics(_select(s, session))
    _emit(ctx, "metrics.skills", data)
    if not is_json(ctx):
        typer.echo(
            f"skills: {data['activations']} activations  deactivated={data['deactivations']}"
        )
        for name, count in data["skills"].items():
            typer.echo(f"  {name}: {count}")


@app.command("agents")
@with_error_handling("metrics.agents")
def metrics_agents(
    ctx: typer.Context,
    session: str = typer.Option(None, "--session", help="Restrict to one session."),
) -> None:
    """Subagent start/stop counts."""
    with services(ctx) as s:
        data = _agent_metrics(_select(s, session))
    _emit(ctx, "metrics.agents", data)
    if not is_json(ctx):
        typer.echo(f"agents: {data['spawned']} spawned  {data['stopped']} stopped")
        for name, row in data["by_agent"].items():
            typer.echo(f"  {name}: started={row['started']}  stopped={row['stopped']}")


@app.command("cost")
@with_error_handling("metrics.cost")
def metrics_cost(
    ctx: typer.Context,
    session: str = typer.Option(None, "--session", help="Restrict to one session."),
) -> None:
    """Token usage. Cost is unknown unless reliable pricing data exists."""
    with services(ctx) as s:
        data = _model_metrics(_select(s, session))
    data["note"] = "cost requires reliable provider pricing data; not invented"
    _emit(ctx, "metrics.cost", data)
    if not is_json(ctx):
        typer.echo(f"tokens: {data['tokens']}")
        typer.echo("cost:    unknown (no reliable pricing data)")


@app.command("latency")
@with_error_handling("metrics.latency")
def metrics_latency(
    ctx: typer.Context,
    session: str = typer.Option(None, "--session", help="Restrict to one session."),
) -> None:
    """Tool and turn latency percentiles."""
    with services(ctx) as s:
        data = _latency_metrics(_select(s, session))
    _emit(ctx, "metrics.latency", data)
    if not is_json(ctx):
        tools = data["tools_ms"]
        typer.echo(f"tools:   p50={tools['p50']}  p95={tools['p95']}  max={tools['max']} ms")
        turns = data["turns_s"]
        typer.echo(f"turns:   avg={turns['avg']}  p95={turns['p95']} s")
        typer.echo("model:   unknown (wall-time not traced)")


@app.command("success")
@with_error_handling("metrics.success")
def metrics_success(
    ctx: typer.Context,
    session: str = typer.Option(None, "--session", help="Restrict to one session."),
) -> None:
    """Task success: gate outcomes, intervention, loops and tool errors."""
    with services(ctx) as s:
        data = _success_metrics(_select(s, session))
    _emit(ctx, "metrics.success", data)
    if not is_json(ctx):
        task = data["task_success"]
        typer.echo(f"turns:    {data['turns']}  kinds={data['turn_kinds']}")
        typer.echo(
            f"gate:     {task['verified_gate_evaluations']}/{task['gate_evaluations']} verified "
            f"(rate={task['rate']})  outcomes={task['outcomes']}"
        )
        human = data["human_intervention"]
        typer.echo(
            f"human:    {human['approval_requests']} requests  "
            f"{human['approved']} approved  {human['denied']} denied"
        )
        typer.echo(
            f"loops:    rate={data['loop_rate']}  tool_error_rate={data['tool_error_rate']}  "
            f"avg_turn_s={data['avg_turn_duration_s']}  compactions={data['compactions']}"
        )


def _print_all(data: dict) -> None:
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
    skills = data["skills"]
    typer.echo(f"skills:    {skills['activations']} activations  {skills['skills']}")
    agents = data["agents"]
    typer.echo(f"agents:    {agents['spawned']} spawned  {agents['stopped']} stopped")
