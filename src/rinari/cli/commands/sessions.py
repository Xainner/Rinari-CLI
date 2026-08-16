"""`rinari session` group plus the top-level `chat` and `resume` commands.

- `rinari chat [prompt]`  -> explicit CHAT session, even inside a repository.
- `rinari resume [ref]`   -> reconcile and continue a session.
- `rinari session new/list/show` manage the session store.
"""

from __future__ import annotations

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.cli.serializers import session_dict
from rinari.cli.session_flow import resume_flow, start_flow

session_app = typer.Typer(help="Manage sessions.", no_args_is_help=True)


@session_app.command("new")
@with_error_handling("session.new")
def session_new(
    ctx: typer.Context,
    name: str = typer.Option(None, "--name", "-n", help="Session title."),
    chat: bool = typer.Option(False, "--chat", help="Force CHAT even inside a detected project."),
) -> None:
    """Create a new session (kind resolved from the current directory)."""
    with services(ctx) as s:
        from pathlib import Path

        record = s.sessions.new(Path.cwd(), title=name, forced_chat=chat)
        if is_json(ctx):
            emit_json(success_envelope("session.new", session_dict(record)))
            return
        typer.echo(f"Session {record.id} created ({record.kind}).")


@session_app.command("list")
@with_error_handling("session.list")
def session_list(
    ctx: typer.Context,
    kind: str = typer.Option(None, "--kind", help="Filter by CHAT or PROJECT."),
    limit: int = typer.Option(50, "--limit", help="Maximum sessions to list."),
) -> None:
    """List sessions (most recent activity first)."""
    with services(ctx) as s:
        records = s.sessions.list(kind=kind, limit=limit)
        if is_json(ctx):
            emit_json(success_envelope("session.list", [session_dict(r) for r in records]))
            return
        if not records:
            typer.echo("No sessions yet.")
            return
        typer.echo(f"{'ID':<20} {'KIND':<8} {'STATE':<8} {'TITLE':<28} LAST ACTIVE")
        for r in records:
            typer.echo(
                f"{r.id:<20} {r.kind:<8} {r.state:<8} {(r.title or ''):<28} {r.last_active_at}"
            )


@session_app.command("show")
@with_error_handling("session.show")
def session_show(ctx: typer.Context, ref: str = typer.Argument(...)) -> None:
    """Show one session's record."""
    with services(ctx) as s:
        record = s.sessions.show(ref)
        if is_json(ctx):
            emit_json(success_envelope("session.show", session_dict(record)))
            return
        import json

        typer.echo(json.dumps(session_dict(record), indent=2, ensure_ascii=False, default=str))


def chat_cmd(
    ctx: typer.Context,
    prompt: list[str] = typer.Argument(None, help="Initial prompt (phase 2 runs the agent loop)."),
) -> None:
    """Start (or continue) an explicit CHAT session, even inside a repository."""
    text = " ".join(prompt or ()).strip() if prompt else None
    start_flow(ctx, text, forced_chat=True, command="chat.start")


def resume_cmd(
    ctx: typer.Context,
    ref: str = typer.Argument(None, help="Session ID (defaults to the session for this context)."),
) -> None:
    """Reconcile and continue a session."""
    resume_flow(ctx, ref, command="session.resume")
