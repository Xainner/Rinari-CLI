"""`rinari checkpoint` group: checkpoints of agent-owned changes (commands.md 35)."""

from __future__ import annotations

import typer

from rinari.cli.deps import is_json, services, with_error_handling

from ..output import emit_json, success_envelope

app = typer.Typer(help="Checkpoint agent-owned changes.", no_args_is_help=True)

ProjectOpt = typer.Option(".", "--project", help="Project directory.")
SessionOpt = typer.Option(None, "--session", help="Session ID (default: latest for project).")


@app.command("create")
@with_error_handling("checkpoint.create")
def create(
    ctx: typer.Context,
    project: str = ProjectOpt,
    label: str = typer.Option("", "--label", "-l"),
    session: str = SessionOpt,
) -> None:
    """Create a checkpoint of the current dirty tree."""
    with services(ctx) as s:
        result = s.checkpoints.create(project, label=label, session_id=session)
        if is_json(ctx):
            emit_json(success_envelope("checkpoint.create", result))
            return
        files = result.get("files", [])
        typer.echo(
            f"checkpoint {result['id']}  agent={result.get('agent_changes', 0)} "
            f"user={result.get('user_owned', 0)} ({len(files)} path(s))"
        )


@app.command("list")
@with_error_handling("checkpoint.list")
def list_cmd(
    ctx: typer.Context,
    project: str = ProjectOpt,
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """List checkpoints for a project."""
    with services(ctx) as s:
        rows = s.checkpoints.list(project)[:limit]
        if is_json(ctx):
            emit_json(success_envelope("checkpoint.list", rows))
            return
        if not rows:
            typer.echo("no checkpoints")
            return
        for row in rows:
            label = f"  {row['label']}" if row.get("label") else ""
            typer.echo(f"{row['id']}  {row.get('created_at', '')}{label}")


@app.command("show")
@with_error_handling("checkpoint.show")
def show(
    ctx: typer.Context,
    checkpoint_id: str = typer.Argument(None, help="Checkpoint ID (default: latest)."),
    project: str = ProjectOpt,
) -> None:
    """Show checkpoint detail."""
    with services(ctx) as s:
        if checkpoint_id is None:
            rows = s.checkpoints.list(project)
            if not rows:
                from rinari.shared.errors import NotFoundError

                raise NotFoundError("No checkpoints found")
            checkpoint_id = rows[0]["id"]
        data = s.checkpoints.show(checkpoint_id)
        if is_json(ctx):
            emit_json(success_envelope("checkpoint.show", data))
            return
        typer.echo(f"{data['id']}  created {data.get('created_at', '')}")
        if data.get("label"):
            typer.echo(f"label:   {data['label']}")
        agent_changes = data.get("agent_changes", 0)
        user_owned = data.get("user_owned", 0)
        typer.echo(f"agent changes: {agent_changes}  user-owned: {user_owned}")
        for entry in data.get("files", []):
            path = entry.get("path") if isinstance(entry, dict) else entry
            typer.echo(f"  {path}")


@app.command("restore")
@with_error_handling("checkpoint.restore")
def restore(
    ctx: typer.Context,
    checkpoint_id: str = typer.Argument(None, help="Checkpoint ID (default: latest)."),
    project: str = ProjectOpt,
    preview: bool = typer.Option(False, "--preview"),
    allow_mixed: bool = typer.Option(False, "--allow-mixed"),
) -> None:
    """Restore agent-owned paths from a checkpoint."""
    with services(ctx) as s:
        result = s.checkpoints.restore(
            project, checkpoint_id=checkpoint_id, preview=preview, allow_mixed=allow_mixed
        )
        if is_json(ctx):
            emit_json(success_envelope("checkpoint.restore", result))
            return
        verb = "would restore" if preview else "restored"
        typer.echo(f"{verb} {result['checkpoint_id']}: {len(result['applied'])} path(s).")
        for path in result["applied"]:
            typer.echo(f"  restored  {path}")
        for item in result["skipped"]:
            typer.echo(f"  skipped   {item['path']}  ({item['reason']})")


@app.command("remove")
@with_error_handling("checkpoint.remove")
def remove(
    ctx: typer.Context,
    checkpoint_id: str = typer.Argument(...),
    project: str = ProjectOpt,
) -> None:
    """Remove a checkpoint record (does not touch the working tree)."""
    with services(ctx) as s:
        removed = s.checkpoints.remove(checkpoint_id)
        if not removed:
            from rinari.shared.errors import NotFoundError

            raise NotFoundError(f"Checkpoint not found: {checkpoint_id}")
        if is_json(ctx):
            emit_json(success_envelope("checkpoint.remove", {"id": checkpoint_id}))
            return
        typer.echo(f"removed {checkpoint_id}")
