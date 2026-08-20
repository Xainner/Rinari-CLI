"""`rinari logs` group: readable logs over session events (commands.md 50)."""

from __future__ import annotations

import json

import typer

from rinari.cli.deps import is_json, services, with_error_handling

from ..output import emit_json, success_envelope
from .trace import _event_row, _pick_session

app = typer.Typer(help="Session logs (event-based).", no_args_is_help=True)


def _fmt(row: dict) -> str:
    payload = json.dumps(row["payload"] or {}, sort_keys=True)
    return f"[{row['created_at']}] {row['type']} {payload}"


@app.command("show")
@with_error_handling("logs.show")
def show(
    ctx: typer.Context,
    session_id: str = typer.Argument(None),
    limit: int = typer.Option(None, "--limit"),
) -> None:
    """Show all events of a session."""
    with services(ctx) as s:
        record = _pick_session(s, session_id, None)
        events = s.ctx.event_repo.list(record.id)
        rows = [_event_row(e) for e in (events[-limit:] if limit else events)]
        if is_json(ctx):
            emit_json(success_envelope("logs.show", {"session": record.id, "events": rows}))
            return
        for row in rows:
            typer.echo(_fmt(row))


@app.command("tail")
@with_error_handling("logs.tail")
def tail(
    ctx: typer.Context,
    session_id: str = typer.Argument(None),
    n: int = typer.Option(20, "-n", help="Lines."),
) -> None:
    """Show the last N events of a session."""
    with services(ctx) as s:
        record = _pick_session(s, session_id, None)
        events = s.ctx.event_repo.list(record.id)
        rows = [_event_row(e) for e in events[-n:]]
        if is_json(ctx):
            emit_json(success_envelope("logs.tail", rows))
            return
        for row in rows:
            typer.echo(_fmt(row))


@app.command("search")
@with_error_handling("logs.search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
    session_id: str = typer.Argument(None),
    limit: int = typer.Option(100, "--limit"),
) -> None:
    """Search events by type or payload text."""
    with services(ctx) as s:
        record = _pick_session(s, session_id, None)
        events = s.ctx.event_repo.list(record.id)
        rows = []
        for event in events:
            haystack = json.dumps(event.payload or {}, sort_keys=True)
            if query.lower() in event.type.lower() or query.lower() in haystack.lower():
                rows.append(_event_row(event))
        rows = rows[-limit:]
        if is_json(ctx):
            emit_json(success_envelope("logs.search", rows))
            return
        if not rows:
            typer.echo(f"no events matching {query!r}")
            return
        for row in rows:
            typer.echo(_fmt(row))


@app.command("export")
@with_error_handling("logs.export")
def export(
    ctx: typer.Context,
    session_id: str = typer.Argument(...),
    out: str = typer.Option(None, "--out", help="Output file (default: stdout)."),
) -> None:
    """Export a session's events as JSON lines."""
    with services(ctx) as s:
        record = _pick_session(s, session_id, None)
        events = s.ctx.event_repo.list(record.id)
        lines = [json.dumps(_event_row(e), sort_keys=True, default=str) for e in events]
        payload = "\n".join(lines) + "\n"
        if out:
            from pathlib import Path

            Path(out).write_text(payload, encoding="utf-8")
            if is_json(ctx):
                emit_json(
                    success_envelope(
                        "logs.export", {"session": record.id, "path": out, "events": len(events)}
                    )
                )
            else:
                typer.echo(f"exported {len(events)} events -> {out}")
            return
        if is_json(ctx):
            emit_json(
                success_envelope("logs.export", {"session": record.id, "events": events and True})
            )
            return
        typer.echo(payload, nl=False)


@app.command("clear")
@with_error_handling("logs.clear")
def clear(
    ctx: typer.Context,
    session_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Delete a session's events (keeps the session and messages)."""
    with services(ctx) as s:
        record = _pick_session(s, session_id, None)
        if record.state != "archived" and not yes:
            answer = typer.prompt("Delete all events for this session?", default="n").lower()
            if answer not in ("y", "yes"):
                typer.echo("cancelled")
                return
        s.ctx.db.execute("DELETE FROM session_events WHERE session_id = ?", (record.id,))
        if is_json(ctx):
            emit_json(success_envelope("logs.clear", {"session": record.id}))
        else:
            typer.echo(f"cleared events for {record.id}")
