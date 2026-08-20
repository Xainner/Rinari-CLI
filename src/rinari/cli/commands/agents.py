"""`rinari agents` group: agent definitions + one-shot runs (commands.md 44)."""

from __future__ import annotations

from pathlib import Path

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.shared.errors import InvalidUsageError, NotFoundError

from ..output import emit_json, success_envelope

app = typer.Typer(help="Inspect and run agent definitions.", no_args_is_help=True)

_NAME_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9-]*$")


def _row(d) -> dict:
    return {
        "name": d.name,
        "description": d.description,
        "objective": d.objective,
        "profile": d.profile,
        "tools": list(d.tool_allowlist),
        "context": d.context_scope,
        "can_delegate": d.can_delegate,
        "provenance": d.provenance,
    }


@app.command("list")
@with_error_handling("agents.list")
def list_cmd(
    ctx: typer.Context,
    project: str = typer.Option(None, "--project", help="Project root for project-scoped agents."),
) -> None:
    """List agent definitions (built-ins + project, trust-gated)."""
    with services(ctx) as s:
        root = Path(project).resolve() if project else Path.cwd()
        agents = s.agents.list(root)
        rows = [_row(a) for name, a in sorted(agents.items())]
        if is_json(ctx):
            emit_json(success_envelope("agents.list", rows))
            return
        for row in rows:
            desc = (row["description"] or "")[:50]
            typer.echo(f"  {row['name']:<14} {row['profile']:<12} ({row['provenance']}) {desc}")


@app.command("available")
@with_error_handling("agents.available")
def available(ctx: typer.Context) -> None:
    """List built-in agent definitions only."""
    from rinari.agents.definition import builtin_agents

    rows = [_row(a) for name, a in sorted(builtin_agents().items())]
    if is_json(ctx):
        emit_json(success_envelope("agents.available", rows))
        return
    for row in rows:
        typer.echo(f"  {row['name']:<14} {row['profile']:<12} {row['description'][:50]}")


@app.command("show")
@with_error_handling("agents.show")
def show(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    project: str = typer.Option(None, "--project"),
) -> None:
    """Show one agent definition."""
    with services(ctx) as s:
        root = Path(project).resolve() if project else Path.cwd()
        agent = s.agents.get(name, root)
        if agent is None:
            raise NotFoundError(f"Agent not found: {name}")
        row = _row(agent)
        row["budget"] = {
            "max_model_calls": agent.budget.max_model_calls,
            "max_tool_calls": agent.budget.max_tool_calls,
            "max_wall_time_s": agent.budget.max_wall_time_s,
        }
        if is_json(ctx):
            emit_json(success_envelope("agents.show", row))
            return
        typer.echo(f"{agent.name}  ({agent.provenance})")
        typer.echo(f"  profile:  {agent.profile}")
        typer.echo(f"  context:  {agent.context_scope}   can_delegate: {agent.can_delegate}")
        typer.echo(f"  tools:    {', '.join(agent.tool_allowlist) or '-'}")
        budget = agent.budget
        typer.echo(
            f"  budget:   model={budget.max_model_calls} tools={budget.max_tool_calls} "
            f"wall={budget.max_wall_time_s}s"
        )
        if agent.description:
            typer.echo(f"  {agent.description}")
        if agent.objective:
            typer.echo(f"  objective: {agent.objective}")


@app.command("validate")
@with_error_handling("agents.validate")
def validate(
    ctx: typer.Context,
    name: str = typer.Argument(None),
    project: str = typer.Option(None, "--project"),
) -> None:
    """Validate agent definitions (profiles, tool allowlists)."""
    with services(ctx) as s:
        root = Path(project).resolve() if project else Path.cwd()
        rows = s.agents.validate(name, root)
        bad = [r for r in rows if not r["ok"]]
        if is_json(ctx):
            emit_json(success_envelope("agents.validate", rows))
            return
        if not rows:
            typer.echo(f"no agent named {name!r}")
            return
        if bad:
            for row in rows:
                for issue in row["issues"]:
                    typer.echo(f"  [x] {issue['message']}", err=True)
            raise typer.Exit(1)
        typer.echo(f"OK: {len(rows)} definition(s) valid")


@app.command("create")
@with_error_handling("agents.create")
def create(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    project: str = typer.Option(".", "--project"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Scaffold a project agent definition file (.rinari/agents/<name>.md)."""
    if not _NAME_RE.match(name):
        raise InvalidUsageError("Agent names must match ^[a-z0-9][a-z0-9-]*$")
    root = Path(project).resolve()
    if not root.is_dir():
        raise NotFoundError(f"Project directory not found: {root}")
    from rinari.agents.registry import builtin_agents

    if name in builtin_agents() and not force:
        raise InvalidUsageError(
            f"Agent name shadows a built-in: {name}",
            hint="--force overrides it project-locally (profile downgraded to read-only).",
        )
    agent_dir = root / ".rinari" / "agents"
    path = agent_dir / f"{name}.md"
    if path.exists() and not force:
        raise InvalidUsageError(f"Agent file exists: {path}")
    agent_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"name: {name}\n"
        "description: TODO one-line description\n"
        "objective: TODO bounded objective for this agent\n"
        "tools:\n"
        "  - fs.read\n"
        "  - search.files\n"
        "profile: read-only\n"
        "context: project\n"
        "can_delegate: false\n"
        "max_model_calls: 25\n"
        "max_tool_calls: 50\n"
        "max_wall_time_s: 300\n"
        "---\n"
        "\n"
        "TODO: write the agent's system prompt / objective details here.\n",
        encoding="utf-8",
    )
    if is_json(ctx):
        emit_json(success_envelope("agents.create", {"name": name, "path": str(path)}))
    else:
        typer.echo(f"created {path}")


@app.command("run")
@with_error_handling("agents.run")
def run(
    ctx: typer.Context,
    agent: str = typer.Argument(...),
    objective: list[str] = typer.Argument(...),
    project: str = typer.Option(None, "--project"),
    timeout: float = typer.Option(600.0, "--timeout"),
) -> None:
    """Run a single agent in a throwaway session (spends model calls)."""
    from pathlib import Path

    from rinari.cli import agent_runtime
    from rinari.shared.errors import RinariError

    text = " ".join(objective).strip()
    with services(ctx) as s:
        started = s.sessions.start(Path(project).resolve() if project else Path.cwd())
        try:
            session = agent_runtime.build_agent_session(s, started.session, interactive=False)
        except RinariError:
            raise
        try:
            agent_id = session.orchestrator.spawn(agent, text, timeout_s=timeout)
            result = session.orchestrator.wait(agent_id, timeout_s=timeout)
        finally:
            session.end()
    usage = result.usage or {}
    usage_model = (
        usage.get("model_calls") if isinstance(usage, dict) else getattr(usage, "model_calls", None)
    )
    usage_tools = (
        usage.get("tool_calls") if isinstance(usage, dict) else getattr(usage, "tool_calls", None)
    )
    data = {
        "agent": agent,
        "id": agent_id,
        "status": result.status,
        "summary": result.summary,
        "files_changed": list(result.files_changed or ()),
        "error": result.error,
        "usage": usage,
    }
    if is_json(ctx):
        emit_json(success_envelope("agents.run", data))
        return
    typer.echo(f"[{result.status}] agent {agent}")
    if result.summary:
        typer.echo()
        typer.echo(result.summary)
    if result.error:
        typer.echo(f"error: {result.error}", err=True)
    if usage_model is not None or usage_tools is not None:
        typer.echo(
            f"model calls: {usage_model}, tool calls: {usage_tools}",
            err=True,
        )


@app.command("stop")
@with_error_handling("agents.stop")
def stop(ctx: typer.Context, agent_id: str = typer.Argument(...)) -> None:
    """Stop a running agent."""
    raise InvalidUsageError(
        "Agent runtime is only available inside an interactive session",
        hint="Use /agents in the REPL to see running agents; cancellation happens at session end.",
    )


@app.command("logs")
@with_error_handling("agents.logs")
def logs(
    ctx: typer.Context,
    session_id: str = typer.Argument(None),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """Show subagent events from a session's trace."""
    with services(ctx) as s:
        if session_id:
            record = s.ctx.session_repo.get(session_id)
            if record is None:
                raise NotFoundError(f"Session not found: {session_id}")
        else:
            candidates = s.ctx.session_repo.list(limit=1)
            if not candidates:
                raise NotFoundError("No sessions found")
            record = candidates[0]
        events = s.ctx.event_repo.list(record.id)
        rows = [
            {
                "seq": e.seq,
                "type": e.type,
                "payload": e.payload,
                "created_at": e.created_at,
            }
            for e in events
            if e.type in ("SubagentStart", "SubagentStop", "AgentSpawned", "TurnCompleted")
        ][-limit:]
        data = {"session": record.id, "events": rows}
        if is_json(ctx):
            emit_json(success_envelope("agents.logs", data))
            return
        if not rows:
            typer.echo(f"no subagent events for session {record.id}")
            return
        for row in rows:
            payload = row["payload"] or {}
            detail = payload.get("agent") or payload.get("agent_id") or ""
            typer.echo(f"  #{row['seq']:<4} {row['type']} {detail}")
