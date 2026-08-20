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
        from rich.console import Console
        from rich.table import Table
        from rich.text import Text

        state_style = {
            "active": "green",
            "interrupted": "yellow",
            "completed": "blue",
            "archived": "dim",
        }
        table = Table(box=None, pad_edge=False)
        table.add_column("ID", no_wrap=True)
        table.add_column("KIND", no_wrap=True)
        table.add_column("STATE", no_wrap=True)
        table.add_column("TITLE", overflow="ellipsis")
        table.add_column("LAST ACTIVE", no_wrap=True)
        for r in records:
            state = Text(r.state, style=state_style.get(r.state))
            table.add_row(r.id, r.kind, state, r.title or "", r.last_active_at or "")
        Console().print(table)


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


@session_app.command("fork")
@with_error_handling("session.fork")
def session_fork(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Source session ID (or unique prefix)."),
    name: str = typer.Option(None, "--name", "-n", help="Title for the forked session."),
) -> None:
    """Fork a session: an independent continuation with state + conversation."""
    with services(ctx) as s:
        started = s.sessions.fork(ref, title=name)
        if is_json(ctx):
            emit_json(success_envelope("session.fork", session_dict(started.session)))
            return
        typer.echo(
            f"Session {started.session.id} forked from {started.session.forked_from} "
            f"({started.session.kind})."
        )


@session_app.command("current")
@with_error_handling("session.current")
def session_current(ctx: typer.Context) -> None:
    """Show the most recently active session for this context."""
    with services(ctx) as s:
        records = s.sessions.list(limit=1)
        if not records:
            typer.echo("no sessions yet")
            return
        record = records[0]
        if is_json(ctx):
            emit_json(success_envelope("session.current", session_dict(record)))
            return
        typer.echo(
            f"{record.id}  {record.kind}  {record.state}  last active {record.last_active_at}"
        )


@session_app.command("rename")
@with_error_handling("session.rename")
def session_rename(
    ctx: typer.Context,
    ref: str = typer.Argument(...),
    title: str = typer.Argument(...),
) -> None:
    """Rename a session's title."""
    with services(ctx) as s:
        record = s.sessions.show(ref)
        record.title = title
        s.ctx.session_repo.update(record)
        if is_json(ctx):
            emit_json(success_envelope("session.rename", session_dict(record)))
            return
        typer.echo(f"renamed {record.id} -> {title}")


@session_app.command("stop")
@with_error_handling("session.stop")
def session_stop(ctx: typer.Context, ref: str = typer.Argument(...)) -> None:
    """Mark a session as stopped (no further turns accepted)."""
    with services(ctx) as s:
        record = s.sessions.show(ref)
        record.state = "stopped"
        s.ctx.session_repo.update(record)
        if is_json(ctx):
            emit_json(success_envelope("session.stop", session_dict(record)))
            return
        typer.echo(f"{record.id} stopped")


@session_app.command("cancel")
@with_error_handling("session.cancel")
def session_cancel(ctx: typer.Context, ref: str = typer.Argument(...)) -> None:
    """Mark a session's last turn as cancelled/interrupted."""
    with services(ctx) as s:
        record = s.sessions.show(ref)
        record.state = "interrupted"
        s.ctx.session_repo.update(record)
        if is_json(ctx):
            emit_json(success_envelope("session.cancel", session_dict(record)))
            return
        typer.echo(f"{record.id} marked interrupted")


@session_app.command("archive")
@with_error_handling("session.archive")
def session_archive(ctx: typer.Context, ref: str = typer.Argument(...)) -> None:
    """Archive a session (hidden from default lists, still resumable by ID)."""
    with services(ctx) as s:
        record = s.sessions.show(ref)
        record.state = "archived"
        s.ctx.session_repo.update(record)
        if is_json(ctx):
            emit_json(success_envelope("session.archive", session_dict(record)))
            return
        typer.echo(f"archived {record.id}")


@session_app.command("delete")
@with_error_handling("session.delete")
def session_delete(
    ctx: typer.Context,
    ref: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Delete a session and its messages/events (irreversible)."""
    with services(ctx) as s:
        record = s.sessions.show(ref)
        if not yes:
            answer = typer.prompt(f"Delete session {record.id} permanently?", default="n").lower()
            if answer not in ("y", "yes"):
                typer.echo("cancelled")
                return
        s.ctx.db.execute("DELETE FROM session_events WHERE session_id = ?", (record.id,))
        s.ctx.db.execute("DELETE FROM session_messages WHERE session_id = ?", (record.id,))
        s.ctx.db.execute("DELETE FROM sessions WHERE id = ?", (record.id,))
        if is_json(ctx):
            emit_json(success_envelope("session.delete", {"id": record.id}))
            return
        typer.echo(f"deleted {record.id}")


@session_app.command("export")
@with_error_handling("session.export")
def session_export(
    ctx: typer.Context,
    ref: str = typer.Argument(None),
    out: str = typer.Option(None, "--out", help="Output file (default: stdout)."),
) -> None:
    """Export a session (record + messages + events) as a JSON document."""
    with services(ctx) as s:
        from rinari.cli.session_export import export_session

        document = export_session(s, ref)
        if out:
            from pathlib import Path

            Path(out).write_text(_json(document), encoding="utf-8")
            if is_json(ctx):
                emit_json(success_envelope("session.export", {"session": ref, "path": out}))
            else:
                typer.echo(f"exported -> {out}")
            return
        typer.echo(_json(document))


@session_app.command("import")
@with_error_handling("session.import")
def session_import(
    ctx: typer.Context,
    path: str = typer.Argument(...),
) -> None:
    """Import a previously exported session document."""
    with services(ctx) as s:
        from pathlib import Path

        from rinari.cli.session_export import import_session

        record = import_session(s, Path(path).read_text(encoding="utf-8"))
        if is_json(ctx):
            emit_json(success_envelope("session.import", session_dict(record)))
            return
        typer.echo(f"imported {record.id} ({record.kind})")


def _json(document) -> str:
    import json

    return json.dumps(document, indent=2, ensure_ascii=False, default=str)


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
