"""`rinari trace` group: inspect agent execution traces (commands.md 49)."""

from __future__ import annotations

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.shared.errors import NotFoundError

from ..output import emit_json, success_envelope


def _pick_session(s, session_id: str | None, project: str | None):
    if session_id:
        record = s.ctx.session_repo.get(session_id)
        if record is None:
            raise NotFoundError(f"Session not found: {session_id}")
        return record
    candidates = s.ctx.session_repo.list(project_id=None, limit=1)
    if not candidates:
        raise NotFoundError("No sessions found", hint="Start a session first (rinari).")
    return candidates[0]


def _event_row(event) -> dict:
    return {
        "seq": event.seq,
        "type": event.type,
        "payload": event.payload,
        "created_at": event.created_at,
    }


@with_error_handling("trace")
def trace(
    ctx: typer.Context,
    session_id: str = typer.Argument(None, help="Session ID (default: most recent)."),
    project: str = typer.Option(
        None, "--project", help="Prefer the most recent session of a project."
    ),
    types: list[str] = typer.Option(None, "--type", "-t", help="Filter event types (repeatable)."),
    limit: int = typer.Option(50, "--limit", help="Max events (from the end)."),
) -> None:
    """Show a session's event trace (newest last)."""
    with services(ctx) as s:
        record = _pick_session(s, session_id, project)
        events = s.ctx.event_repo.list(record.id)
        if types:
            wanted = set(types)
            events = [e for e in events if e.type in wanted]
        rows = [_event_row(e) for e in events[-limit:]]
        data = {"session": record.id, "events": rows}
        if is_json(ctx):
            emit_json(success_envelope("trace", data))
            return
        typer.echo(f"session {record.id}  ({len(rows)} events)")
        for row in rows:
            detail = ""
            payload = row["payload"] or {}
            for key in ("tool", "agent", "agent_id", "model", "outcome", "error", "path"):
                if key in payload:
                    detail = f" {str(payload[key])[:60]}"
                    break
            typer.echo(f"  #{row['seq']:<4} {row['type']}{detail}")
